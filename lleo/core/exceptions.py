"""
Custom exceptions for LLEO package
"""


class LLEOException(Exception):
    """Base exception for all LLEO errors"""
    pass


class ProcessorException(LLEOException):
    """Exception raised during processor execution"""
    pass


class ValidationException(LLEOException):
    """Exception raised when input validation fails"""
    pass


class EarthEngineException(LLEOException):
    """Exception raised during Earth Engine operations"""
    pass


class RoutingException(LLEOException):
    """Exception raised during query routing"""
    pass


class GeocodingException(LLEOException):
    """Exception raised during geocoding"""
    pass
