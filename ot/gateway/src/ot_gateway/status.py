"""Tradução de resultados Modbus para StatusCodes OPC UA (docs/opcua-address-space.md, secção 4)."""
from asyncua import ua

from .config import SignalConfig
from .modbus_link import LinkError, ModbusExceptionReply, NoResponse

GOOD = ua.StatusCodes.Good


def status_for_error(error: LinkError) -> int:
    """Uma falha nunca é Good. Sem resposta é BadNoCommunication."""
    if isinstance(error, NoResponse):
        return ua.StatusCodes.BadNoCommunication
    if isinstance(error, ModbusExceptionReply):
        if error.code == 0x02:
            return ua.StatusCodes.BadConfigurationError
        if error.code in (0x04, 0x06):
            return ua.StatusCodes.BadDeviceFailure
    return ua.StatusCodes.BadCommunicationError


def status_for_value(signal: SignalConfig, value: float) -> int:
    if signal.eu is not None and not signal.eu[0] <= value <= signal.eu[1]:
        return ua.StatusCodes.UncertainEngineeringUnitsExceeded
    return GOOD


def is_bad(code: int) -> bool:
    return ua.StatusCode(code).is_bad()
