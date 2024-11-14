import psycopg

from aleo_types import *
from aleo_types.cached import cached_get_key_id, cached_get_mapping_id
from db import Database
from interpreter.finalizer import execute_finalizer, ExecuteError, mapping_cache_read, profile
from interpreter.utils import FinalizeState
from util.global_cache import global_mapping_cache, global_program_cache, MappingCacheDict, get_program


async def init_builtin_program(db: Database, program: Program):
    for mapping in program.mappings.keys():
        mapping_id = Field.loads(cached_get_mapping_id(str(program.id), str(mapping)))
        await db.initialize_builtin_mapping(str(mapping_id), str(program.id), str(mapping))
        if await db.get_program(str(program.id)) is None:
            await db.save_builtin_program(program)

async def _execute_public_fee(db: Database, cur: psycopg.AsyncCursor[dict[str, Any]], finalize_state: FinalizeState,
                              fee_transition: Transition, mapping_cache: dict[Field, MappingCacheDict],
                              local_mapping_cache: dict[Field, MappingCacheDict], allow_state_change: bool
                              ) -> list[dict[str, Any]]:
    if fee_transition.program_id != "credits.aleo" or fee_transition.function_name != "fee_public":
        raise TypeError("not a fee transition")
    output = fee_transition.outputs[0]
    if not isinstance(output, FutureTransitionOutput):
        raise TypeError("invalid fee transition output")
    future = output.future.value
    if future is None:
        raise RuntimeError("invalid fee transition output")
    program = await get_program(db, str(future.program_id))
    if not program:
        raise RuntimeError("program not found")

    inputs: list[Value] = load_input_from_arguments(future.arguments)
    return await execute_finalizer(db, cur, finalize_state, [fee_transition.id], set(), program, future.function_name, inputs,
                                   mapping_cache, local_mapping_cache, allow_state_change)

async def finalize_deploy(db: Database, cur: psycopg.AsyncCursor[dict[str, Any]], finalize_state: FinalizeState,
                          confirmed_transaction: ConfirmedTransaction, mapping_cache: dict[Field, MappingCacheDict]
                          ) -> tuple[list[FinalizeOperation], list[dict[str, Any]], Optional[str]]:
    transaction = confirmed_transaction.transaction
    if isinstance(transaction, (DeployTransaction, FeeTransaction)):
        transition = cast(Fee, transaction.fee).transition
    else:
        raise NotImplementedError
    if transition.function_name == "fee_public":
        operations = await _execute_public_fee(db, cur, finalize_state, transition, mapping_cache, {}, True)
    else:
        operations: list[dict[str, Any]] = []

    if isinstance(confirmed_transaction, AcceptedDeploy):
        deployment = cast(DeployTransaction, transaction).deployment
        program = deployment.program
        expected_operations = confirmed_transaction.finalize
        for mapping in program.mappings.keys():
            mapping_id = Field.loads(cached_get_mapping_id(str(program.id), str(mapping)))
            operations.append({
                "type": FinalizeOperation.Type.InitializeMapping,
                "mapping_id": mapping_id,
                "program_id": program.id,
                "mapping": mapping,
            })
        rejected_reason = None
    else:
        expected_operations = confirmed_transaction.finalize
        rejected_reason = "(detailed reason not available)"
    return expected_operations, operations, rejected_reason

def load_input_from_arguments(arguments: list[Argument]) -> list[Value]:
    inputs: list[Value] = []
    for argument in arguments:
        if isinstance(argument, PlaintextArgument):
            inputs.append(PlaintextValue(plaintext=argument.plaintext))
        elif isinstance(argument, FutureArgument):
            inputs.append(FutureValue(future=argument.future))
        else:
            raise NotImplementedError
    return inputs

LocalCallGraphNode = TypedDict("LocalCallGraphNode", {"index": int, "name": str, "transition_id": TransitionID, "calls": list["LocalCallGraphNode"]})

async def _trace_execution(db: Database, transition_ids: list[TransitionID], program: Program, function_name: Identifier, node: Program.CallGraphNode, async_order: list[TransitionID]):
    # add transition ids to call graph
    for c in node["calls"]:
        cast(LocalCallGraphNode, c)["transition_id"] = transition_ids[c["index"]]
    future_register_map: dict[int, LocalCallGraphNode] = {}
    finalize_register_map: dict[int, LocalCallGraphNode] = {}
    function = program.functions[function_name]
    if function.finalize.value is None:
        return
    call_index = 0
    for inst in function.instructions:
        if isinstance(inst.literals, CallInstruction) and isinstance(inst.literals.operator, LocatorCallOperator):
            locator = inst.literals.operator.locator
            called_program = await get_program(db, str(locator.id))
            if not called_program:
                raise RuntimeError("program not found")
            called_function = called_program.functions[locator.resource]
            if called_function.finalize.value is None:
                call_index += 1
                continue
            future_destination = cast(LocatorRegister, inst.literals.destinations[-1])
            future_register_map[future_destination.locator] = cast(LocalCallGraphNode, node["calls"][call_index])
            if f"{called_program.id}/{called_function.name}" != node["calls"][call_index]["name"]:
                raise RuntimeError("call analysis failed")
            call_index += 1
        elif isinstance(inst.literals, AsyncInstruction):
            for param_index, operand in enumerate(inst.literals.operands):
                if isinstance(operand, RegisterOperand) and isinstance(operand.register, LocatorRegister):
                    if operand.register.locator in future_register_map:
                        finalize_register_map[param_index] = future_register_map[operand.register.locator]

    for cmd in function.finalize.value.commands:
        if isinstance(cmd, AwaitCommand):
            locator = cmd.register.locator
            if locator not in finalize_register_map:
                raise RuntimeError("invalid await command")
            called_node = finalize_register_map[locator]
            async_order.append(called_node["transition_id"])
            p, f = called_node["name"].split("/")
            called_program = await get_program(db, p)
            if not called_program:
                raise RuntimeError("program not found")
            if Identifier.loads(f) not in called_program.functions:
                raise RuntimeError("function not found")
            await _trace_execution(db, transition_ids, called_program, Identifier.loads(f), called_node, async_order)


async def build_async_order(db: Database, transition_ids: list[TransitionID], program: Program, function_name: Identifier) -> list[TransitionID]:
    call_graph = await program.call_graph(function_name, db)

    root_call_graph: Program.CallGraphNode = {
        "index": len(transition_ids) - 1,
        "name": "",
        "calls": call_graph,
    }

    async_order = [transition_ids[-1]]

    await _trace_execution(db, transition_ids, program, function_name, root_call_graph, async_order)

    return async_order

@profile
async def finalize_execute(db: Database, cur: psycopg.AsyncCursor[dict[str, Any]], finalize_state: FinalizeState,
                           confirmed_transaction: ConfirmedTransaction, mapping_cache: dict[Field, MappingCacheDict]
                           ) -> tuple[list[FinalizeOperation], list[dict[str, Any]], Optional[str]]:
    expected_operations = list(confirmed_transaction.finalize)
    if isinstance(confirmed_transaction, AcceptedExecute):
        transaction = confirmed_transaction.transaction
        if not isinstance(transaction, ExecuteTransaction):
            raise TypeError("invalid execute transaction")
        execution = transaction.execution
        allow_state_change = True
        local_mapping_cache = mapping_cache
        fee = cast(Option[Fee], transaction.fee).value
    elif isinstance(confirmed_transaction, RejectedExecute):
        if not isinstance(confirmed_transaction.rejected, RejectedExecution):
            raise TypeError("invalid rejected execute transaction")
        execution = confirmed_transaction.rejected.execution
        allow_state_change = False
        local_mapping_cache = {}
        if not isinstance(confirmed_transaction.transaction, FeeTransaction):
            raise TypeError("invalid rejected execute transaction")
        fee = cast(Fee, confirmed_transaction.transaction.fee)
    else:
        raise NotImplementedError
    operations: list[dict[str, Any]] = []
    reject_reason: str | None = None
    transition = execution.transitions[-1]
    if len(transition.outputs) != 0:
        maybe_future_output = transition.outputs[-1]
        if isinstance(maybe_future_output, FutureTransitionOutput):
            future_option = maybe_future_output.future
            if future_option.value is None:
                raise RuntimeError("invalid future is None")
            future = future_option.value
            program = await get_program(db, str(future.program_id))
            if not program:
                raise RuntimeError("program not found")

            transition_ids = [x.id for x in execution.transitions]

            async_order = await build_async_order(db, transition_ids, program, future.function_name)

            inputs: list[Value] = load_input_from_arguments(future.arguments)
            try:
                operations.extend(
                    await execute_finalizer(db, cur, finalize_state, async_order, set(), program, future.function_name, inputs, mapping_cache, local_mapping_cache, allow_state_change)
                )
            except ExecuteError as e:
                for ts in execution.transitions:
                    if ts.id == e.transition_id:
                        index = execution.transitions.index(ts)
                        break
                else:
                    raise RuntimeError("rejected transition not found in transaction")
                reject_reason = f"execute error: {e}, at transition #{index}, instruction \"{e.instruction}\""
                operations = []
    if isinstance(confirmed_transaction, RejectedExecute) and reject_reason is None:
        # execute fee as part of rejected execute as it failed here
        # this is the case when the account doesn't have enough credits for fee after executing other transitions
        transition = cast(Fee, fee).transition
        if transition.function_name == "fee_public":
            try:
                operations.extend(await _execute_public_fee(db, cur, finalize_state, transition, mapping_cache, local_mapping_cache, allow_state_change))
            except ExecuteError as e:
                reject_reason = f"execute error: {e}, at fee transition, instruction \"{e.instruction}\""
                operations = []
            else:
                # well we don't really know the reason, but have to continue
                reject_reason = "unknown reason"
                operations = []
        else:
            # same as above but for private fee
            reject_reason = "unknown reason"
            operations = []

    if fee:
        transition = fee.transition
        if transition.function_name == "fee_public":
            # failure means aborted transaction, so this is bound to succeed
            operations.extend(await _execute_public_fee(db, cur, finalize_state, transition, mapping_cache, local_mapping_cache, True))
    return expected_operations, operations, reject_reason

@profile
async def finalize_block(db: Database, cur: psycopg.AsyncCursor[dict[str, Any]], block: Block) -> list[Optional[str]]:
    finalize_state = FinalizeState(block)
    reject_reasons: list[Optional[str]] = []
    for confirmed_transaction in block.transactions.transactions:
        confirmed_transaction: ConfirmedTransaction
        CTType = ConfirmedTransaction.Type
        if confirmed_transaction.type in [CTType.AcceptedDeploy, CTType.RejectedDeploy]:
            expected_operations, operations, reject_reason = await finalize_deploy(db, cur, finalize_state, confirmed_transaction, global_mapping_cache)
        elif confirmed_transaction.type in [CTType.AcceptedExecute, CTType.RejectedExecute]:
            expected_operations, operations, reject_reason = await finalize_execute(db, cur, finalize_state, confirmed_transaction, global_mapping_cache)
        else:
            raise NotImplementedError

        if len(expected_operations) != len(operations):
            print("expected:", expected_operations)
            print("actual:", operations)
            print(reject_reason)
            raise TypeError("invalid finalize operation length")

        for e, o in zip(expected_operations, operations):
            try:
                if e.type != o["type"]:
                    raise TypeError("invalid finalize operation type")
                if e.mapping_id != o["mapping_id"]:
                    raise TypeError("invalid finalize mapping id")
                if isinstance(e, InitializeMapping):
                    pass
                elif isinstance(e, UpdateKeyValue):
                    if e.key_id != o["key_id"] or e.value_id != o["value_id"]:
                        raise TypeError("invalid finalize update key operation")
                elif isinstance(e, RemoveKeyValue):
                    if e.key_id != o["key_id"]:
                        raise TypeError("invalid finalize remove key operation")
                else:
                    raise NotImplementedError
            except TypeError:
                from pprint import pprint
                print("expected:", e.__dict__)
                print("actual:", o)
                global_mapping_cache.clear()
                raise

        await execute_operations(db, cur, operations)
        reject_reasons.append(reject_reason)
    return reject_reasons


async def execute_operations(db: Database, cur: psycopg.AsyncCursor[dict[str, Any]], operations: list[dict[str, Any]]):
    for operation in operations:
        match operation["type"]:
            case FinalizeOperation.Type.InitializeMapping:
                mapping_id = operation["mapping_id"]
                program_id = operation["program_id"]
                mapping = operation["mapping"]
                await db.initialize_mapping(cur, str(mapping_id), str(program_id), str(mapping))
            case FinalizeOperation.Type.UpdateKeyValue:
                mapping_id = operation["mapping_id"]
                key_id = operation["key_id"]
                value_id = operation["value_id"]
                key = operation["key"]
                value = operation["value"]
                program_name = operation["program_name"]
                mapping_name = operation["mapping_name"]
                from_transaction = operation["from_transaction"]
                await db.update_mapping_key_value(cur, program_name, mapping_name, str(mapping_id), str(key_id), str(value_id), key.dump(), value.dump(), operation["height"], from_transaction)
            case FinalizeOperation.Type.RemoveKeyValue:
                mapping_id = operation["mapping_id"]
                key_id = operation["key_id"]
                key = operation["key"]
                program_name = operation["program_name"]
                mapping_name = operation["mapping_name"]
                from_transaction = operation["from_transaction"]
                height = operation["height"]
                await db.remove_mapping_key_value(cur, program_name, mapping_name, str(mapping_id), str(key_id), key.dump(), height, from_transaction)
            case _:
                raise NotImplementedError

async def get_mapping_value(db: Database, program_id: str, mapping_name: str, key: str) -> Value:
    # where was this used?
    mapping_id = Field.loads(cached_get_mapping_id(program_id, mapping_name))
    if mapping_id not in global_mapping_cache:
        global_mapping_cache[mapping_id] = await mapping_cache_read(db, program_id, mapping_name)
    if str(program_id) in global_program_cache:
        program = global_program_cache[str(program_id)]
    else:
        program_bytes = await db.get_program(str(program_id))
        if program_bytes is None:
            raise RuntimeError("program not found")
        program = Program.load(BytesIO(program_bytes))
        global_program_cache[str(program_id)] = program
    mapping = program.mappings[Identifier(value=mapping_name)]
    mapping_key_type = mapping.key.plaintext_type
    if not isinstance(mapping_key_type, LiteralPlaintextType):
        raise TypeError("unsupported key type")
    key_plaintext = LiteralPlaintext(literal=Literal.loads(Literal.Type(mapping_key_type.literal_type.value), key))
    key_id = Field.loads(cached_get_key_id(program_id, mapping_name, key_plaintext.dump()))
    if key_id not in global_mapping_cache[mapping_id]:
        raise ExecuteError(f"key {key} not found in mapping {mapping_id}", None, "", )
    else:
        value = global_mapping_cache[mapping_id][key_id]["value"]
        if not isinstance(value, PlaintextValue):
            raise TypeError("invalid value type")
    return value

async def preview_finalize_execution(db: Database, program: Program, function_name: Identifier, inputs: list[Value]) -> list[dict[str, Any]]:
    block = await db.get_latest_block()
    finalize_state = FinalizeState(block)
    return await execute_finalizer(
        db,
        None,
        finalize_state,
        TransitionID.load(BytesIO(b"\x00" * 32)),
        program,
        function_name,
        inputs,
        mapping_cache={},
        local_mapping_cache={},
        allow_state_change=False,
        execute_await=True,
    )