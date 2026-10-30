"""The ``manage_connections`` card on the session authority: ``connection.respond`` and the operation's ``status``/``wake``, plus the open card a resume restores (``pending_connection``).

The operation lives in ``tools/connectors/live.py`` under the turn's session key (the live session's route) and profile, so a card can only drive its own session's operation. Answers and refusals share that file's code (``tools.connectors.run.respond``, ``tui_gateway/connector_refusals.py``); ``ConnectorRefusal`` carries a refusal through the authority's error path.
"""
import asyncio
from functools import partial

from noesia_state_runtime import RuntimeStoreError

METHODS = ('connection.respond', 'connectors.operation.status', 'connectors.operation.wake')


class ConnectorRefusal(RuntimeStoreError):
    """A refusal on the legacy connector handlers' wire shape: their code, their human message, their ``ConnectorErrorReason`` as ``data.reason``."""

    def __init__(self, code, reason, message):
        super().__init__(str(reason), detail=message)
        self.wire_code, self.wire_message = code, message


def session_owned(params) -> bool:
    owner = params.get('owner') if isinstance(params, dict) else None
    return isinstance(owner, dict) and owner.get('type') == 'session'


def handlers(connection):
    return {method: partial(_handle, connection, method) for method in METHODS}


def pending_connection(authority, session_id):
    """The open operation of a live session as its ``connection.request`` payload, or None."""
    from tools.connectors import live
    operation = live.current(authority.sessions[session_id].route, profile_home=authority.profile_id)
    return operation.request_payload() if operation is not None else None


def _request(method, params):
    """The validated envelope and, for ``connection.respond``, the answer validated on its own as the legacy handler does: a bad answer is ``INVALID_ANSWER``, not a bad envelope."""
    from pydantic import ValidationError
    from tui_gateway.connector_refusals import INVALID_ANSWER, INVALID_PARAMS
    from tui_gateway.contracts.connectors_operation import ConnectionAnswer, ConnectionOperationParams
    from tui_gateway.contracts.registry import METHODS as CONTRACTS
    respond = method == 'connection.respond'
    contract = ConnectionOperationParams if respond else CONTRACTS[method].params
    try:
        request = contract.model_validate({k: v for k, v in params.items() if not (respond and k == 'result')})
    except ValidationError as exc:
        raise ConnectorRefusal(*INVALID_PARAMS) from exc
    if not respond:
        return request, None
    try:
        return request, ConnectionAnswer.model_validate(params.get('result'))
    except ValidationError as exc:
        raise ConnectorRefusal(*INVALID_ANSWER) from exc


def _view(operation):
    from tui_gateway.connector_payload import connector_ui_payload
    snapshot = operation.snapshot()
    return connector_ui_payload({**snapshot, 'settled': snapshot.get('settled_at') is not None})


async def _handle(connection, method, _ref, params):
    from gateway.session_busy_controls import authorize
    from gateway.session_contract import SessionRef
    from tools.connectors import live
    from tui_gateway import connector_refusals as refusals
    request, answer = _request(method, params)
    authority = connection.authority
    ref = SessionRef(authority.profile_id, request.owner.session_id)
    # Capability before existence, as every authority verb: a refused caller learns nothing about the id.
    authorize(connection, ref, params, 'session:read' if method == 'connectors.operation.status' else 'session:control')
    if ref.session_id not in authority.sessions:
        raise RuntimeStoreError('not_found')
    route = authority.sessions[ref.session_id].route
    operation = live.get(route, request.op_id, profile_home=authority.profile_id)
    if operation is None:
        raise ConnectorRefusal(*refusals.UNKNOWN_OPERATION)
    if method == 'connectors.operation.status':
        return _view(operation)
    if method == 'connectors.operation.wake':
        operation.wake.set()
        return {'status': 'ok'}
    return await asyncio.to_thread(_respond, operation, answer)


def _respond(operation, answer):
    from tools.connectors.operation import IllegalTransition
    from tools.connectors.run import respond
    from tui_gateway.connector_refusals import INVALID_ANSWER
    try:
        return {'status': 'ok', 'settled': respond(operation, answer.model_dump_json(exclude_none=True))}
    except IllegalTransition as exc:
        raise ConnectorRefusal(*INVALID_ANSWER) from exc
