"""
Base processor class for all LLEO analysis types
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
import logging

from .config import LLEOConfig
from .exceptions import ValidationException, ProcessorException

logger = logging.getLogger(__name__)


@dataclass
class AnalysisRequest:
    """Standardized analysis request"""
    session_id: str
    user_id: str
    query: str
    location: Dict[str, Any]  # {name, latitude, longitude, country, etc.}
    time_period: Dict[str, Any]  # {start_year, end_year, start_date, end_date}
    analysis_type: str
    parameters: Optional[Dict[str, Any]] = None


@dataclass
class AnalysisResult:
    """Standardized analysis result"""
    session_id: str
    status: str  # 'completed', 'failed', 'partial'
    analysis_type: str
    location: Dict[str, Any]
    time_period: Dict[str, Any]
    outputs: Dict[str, str]  # {map: url, charts: url, preview: url, data: url}
    metadata: Dict[str, Any]
    error: Optional[str] = None
    processing_time: Optional[float] = None


class BaseProcessor(ABC):
    """
    Base class for all analysis processors

    Each domain (NDVI, Air Quality, Water Quality, etc.) implements this interface
    """

    def __init__(self, config: LLEOConfig):
        """
        Initialize processor with configuration

        Args:
            config: LLEO configuration object
        """
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def get_analysis_type(self) -> str:
        """
        Return the analysis type identifier

        Returns:
            Analysis type string (e.g., 'ndvi', 'air_quality')
        """
        pass

    @abstractmethod
    def get_required_parameters(self) -> List[str]:
        """
        Return list of required parameters for this analysis

        Returns:
            List of required parameter names
        """
        pass

    @abstractmethod
    def validate_request(self, request: AnalysisRequest) -> bool:
        """
        Validate analysis request parameters

        Args:
            request: Analysis request to validate

        Returns:
            True if valid, raises ValidationException otherwise
        """
        pass

    @abstractmethod
    async def process(self, request: AnalysisRequest) -> AnalysisResult:
        """
        Process the analysis request

        Args:
            request: Analysis request with all parameters

        Returns:
            Analysis result with outputs and metadata
        """
        pass

    def validate_location(self, location: Dict[str, Any]) -> bool:
        """
        Validate location data

        Args:
            location: Location dictionary

        Returns:
            True if valid

        Raises:
            ValidationException: If location is invalid
        """
        required_fields = ['latitude', 'longitude']
        for field in required_fields:
            if field not in location:
                raise ValidationException(f"Missing required location field: {field}")

        # Validate coordinates
        lat = location['latitude']
        lon = location['longitude']

        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValidationException("Latitude and longitude must be numbers")

        if not (-90 <= lat <= 90):
            raise ValidationException(f"Invalid latitude: {lat} (must be -90 to 90)")

        if not (-180 <= lon <= 180):
            raise ValidationException(f"Invalid longitude: {lon} (must be -180 to 180)")

        return True

    def validate_time_period(self, time_period: Dict[str, Any]) -> bool:
        """
        Validate time period data

        Args:
            time_period: Time period dictionary

        Returns:
            True if valid

        Raises:
            ValidationException: If time period is invalid
        """
        # Check if we have years or dates
        has_years = 'start_year' in time_period and 'end_year' in time_period
        has_dates = 'start_date' in time_period and 'end_date' in time_period

        if not (has_years or has_dates):
            raise ValidationException("Time period must have either years or dates")

        if has_years:
            start_year = time_period['start_year']
            end_year = time_period['end_year']

            if not isinstance(start_year, int) or not isinstance(end_year, int):
                raise ValidationException("Years must be integers")

            current_year = datetime.now().year
            if not (2015 <= start_year <= current_year):
                raise ValidationException(f"Start year {start_year} out of range (2015-{current_year})")

            if not (2015 <= end_year <= current_year):
                raise ValidationException(f"End year {end_year} out of range (2015-{current_year})")

            if start_year > end_year:
                raise ValidationException("Start year cannot be after end year")

        return True

    def create_result(
        self,
        request: AnalysisRequest,
        status: str,
        outputs: Dict[str, str],
        metadata: Dict[str, Any],
        error: Optional[str] = None,
        processing_time: Optional[float] = None
    ) -> AnalysisResult:
        """
        Create standardized analysis result

        Args:
            request: Original analysis request
            status: Result status
            outputs: Output files/URLs
            metadata: Analysis metadata
            error: Error message if failed
            processing_time: Processing time in seconds

        Returns:
            AnalysisResult object
        """
        return AnalysisResult(
            session_id=request.session_id,
            status=status,
            analysis_type=self.get_analysis_type(),
            location=request.location,
            time_period=request.time_period,
            outputs=outputs,
            metadata=metadata,
            error=error,
            processing_time=processing_time
        )

    def log_info(self, message: str, **kwargs):
        """Log info message with processor context"""
        self.logger.info(f"[{self.get_analysis_type()}] {message}", extra=kwargs)

    def log_error(self, message: str, **kwargs):
        """Log error message with processor context"""
        self.logger.error(f"[{self.get_analysis_type()}] {message}", extra=kwargs)

    def log_warning(self, message: str, **kwargs):
        """Log warning message with processor context"""
        self.logger.warning(f"[{self.get_analysis_type()}] {message}", extra=kwargs)
