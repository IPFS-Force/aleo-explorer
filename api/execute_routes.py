from io import BytesIO

from starlette.requests import Request
from starlette.responses import JSONResponse

from db import Database
from interpreter.finalizer import ExecuteError
from interpreter.interpreter import preview_finalize_execution
from node.types import Program, Identifier, Function, Finalize, FinalizeInput, PlaintextType, LiteralPlaintextType, \
    LiteralPlaintext, Literal, StructPlaintextType, StructPlaintext, FinalizeOperation


async def preview_finalize_route(request: Request):
    db: Database = request.app.state.db
    version = request.path_params["version"]
    json = await request.json()
    program_id = json.get("program_id")
    transition_name = json.get("transition_name")
    inputs = json.get("inputs")
    if not program_id:
        return JSONResponse({"error": "Missing program_id"}, status_code=400)
    if not transition_name:
        return JSONResponse({"error": "Missing transition_name"}, status_code=400)
    if inputs is None:
        return JSONResponse({"error": "Missing inputs (pass empty array for no input)"}, status_code=400)
    if not isinstance(inputs, list):
        return JSONResponse({"error": "Inputs must be an array"}, status_code=400)

    try:
        program = Program.load(BytesIO(await db.get_program(program_id)))
    except:
        return JSONResponse({"error": "Program not found"}, status_code=404)
    function_name = Identifier.loads(transition_name)
    if function_name not in program.functions:
        return JSONResponse({"error": "Transition not found"}, status_code=404)
    function: Function = program.functions[function_name]
    if function.finalize.value is None:
        return JSONResponse({"error": "Transition does not have a finalizer"}, status_code=400)
    finalize: Finalize = function.finalize.value[1]
    finalize_inputs = finalize.inputs
    values = []
    for index, finalize_input in enumerate(finalize_inputs):
        finalize_input: FinalizeInput
        plaintext_type: PlaintextType = finalize_input.plaintext_type
        if plaintext_type.type == PlaintextType.Type.Literal:
            plaintext_type: LiteralPlaintextType
            primitive_type = plaintext_type.literal_type.primitive_type
            try:
                value = primitive_type.loads(str(inputs[index]))
            except:
                return JSONResponse({"error": f"Invalid input for index {index}"}, status_code=400)
            values.append(LiteralPlaintext(literal=Literal(type_=Literal.reverse_primitive_type_map[primitive_type], primitive=value)))
        elif plaintext_type.type == PlaintextType.Type.Struct:
            plaintext_type: StructPlaintextType
            structs = program.structs
            struct_type = structs[plaintext_type.struct]
            try:
                value = StructPlaintext.loads(inputs[index], struct_type, structs)
            except Exception as e:
                return JSONResponse({"error": f"Invalid input for index {index}: {e} (experimental feature, if you believe this is an error please submit a feedback)"}, status_code=400)
            values.append(value)
        else:
            return JSONResponse({"error": "Unknown input type"}, status_code=500)
    try:
        result = await preview_finalize_execution(db, program, function_name, values)
    except ExecuteError as e:
        return JSONResponse({"error": f"Execution error on instruction \"{e.instruction}\": {e.original_exception}"}, status_code=400)
    updates = []
    for operation in result:
        operation_type = operation["type"]
        upd = {"type": operation_type.name}
        if operation_type == FinalizeOperation.Type.InitializeMapping:
            raise RuntimeError("InitializeMapping should not be returned by preview_finalize_execution (only used in deployments)")
        elif operation_type == FinalizeOperation.Type.InsertKeyValue:
            raise RuntimeError("InsertKeyValue should not be returned by preview_finalize_execution (only used in tests)")
        elif operation_type == FinalizeOperation.Type.UpdateKeyValue:
            upd.update({
                "mapping_id": str(operation["mapping_id"]),
                "index": operation["index"],
                "key_id": str(operation["key_id"]),
                "value_id": str(operation["value_id"]),
                "mapping": str(operation["mapping"]),
                "key": str(operation["key"]),
                "value": str(operation["value"]),
            })
        elif operation_type == FinalizeOperation.Type.RemoveKeyValue:
            raise NotImplementedError("operation not implemented in the interpreter")
        elif operation_type == FinalizeOperation.Type.RemoveMapping:
            raise RuntimeError("RemoveMapping should not be returned by preview_finalize_execution (only used in tests)")
        else:
            raise RuntimeError("Unknown operation type")
        updates.append(upd)
    return JSONResponse({"mapping_updates": updates})