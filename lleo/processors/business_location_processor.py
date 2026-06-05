"""
Business Location Processor for LLEO
Finds and analyzes business locations using Google Places API
"""

import os
import time
import requests
import json
import numpy as np
from typing import Dict, Any, List
from ..core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from ..core.exceptions import ValidationException, ProcessorException
from ..visualization.business_location_visualization import BusinessLocationVisualization


class BusinessLocationProcessor(BaseProcessor):
    """
    Business Location Analysis Processor

    Uses Google Places API to:
    - Search for businesses by type and location
    - Analyze business density and distribution
    - Provide ratings and reviews analysis
    """

    def __init__(self, config=None):
        super().__init__(config)

        # API key will be loaded lazily when needed
        self._api_key = None
        self._api_key_loaded = False

        # Use searchText endpoint for flexible queries
        self.search_text_url = "https://places.googleapis.com/v1/places:searchText"

    @property
    def api_key(self) -> str:
        """Lazy-load Google Places API key from environment or Secret Manager"""
        if not self._api_key_loaded:
            self._api_key_loaded = True

            # Try environment variable first
            api_key = os.getenv('GOOGLE_PLACES_API_KEY')

            if api_key:
                api_key = api_key.strip()
                print(f"✅ Google Places API key loaded from environment (length: {len(api_key)})")
                self._api_key = api_key
                return self._api_key

            # Try getting from Secret Manager if get_secret function is available
            if hasattr(self, 'get_secret') and self.get_secret:
                try:
                    api_key = self.get_secret('google-places-key')
                    if api_key:
                        api_key = api_key.strip()
                        print(f"✅ Google Places API key loaded from Secret Manager (length: {len(api_key)})")
                        self._api_key = api_key
                        return self._api_key
                except Exception as e:
                    print(f"⚠️  Could not load API key from Secret Manager: {e}")

            print("⚠️  No Google Places API key found, will use mock data")
            self._api_key = None

        return self._api_key

    def get_analysis_type(self) -> str:
        return "business_location"

    def get_required_parameters(self) -> List[str]:
        """Return required parameters for business location analysis"""
        return ['location']

    def validate_request(self, request: AnalysisRequest) -> None:
        """Validate business location analysis request"""
        super().validate_request(request)

        # Business location analysis requires location
        if not request.location:
            raise ValidationException("Business location analysis requires a location")

        # Extract business parameters
        parameters = request.parameters or {}
        business_type = parameters.get('business_type', '')
        keyword = parameters.get('keyword', '')

        if not business_type and not keyword:
            raise ValidationException("Either business_type or keyword must be provided")

    def process(self, request: AnalysisRequest) -> AnalysisResult:
        """Process business location analysis"""
        start_time = time.time()

        try:
            self.log_info("Starting business location analysis", session_id=request.session_id)

            # Extract parameters
            location = request.location
            parameters = request.parameters or {}

            business_type = parameters.get('business_type', '')
            keyword = parameters.get('keyword', '')
            radius = parameters.get('radius', 2000)  # Default 2km radius

            self.log_info(
                f"Searching for: {keyword or business_type} within {radius}m",
                session_id=request.session_id
            )

            # Search for businesses
            businesses = self._search_businesses(location, parameters, request.session_id)

            if not businesses:
                self.log_warning("No businesses found", session_id=request.session_id)

            self.log_info(f"Found {len(businesses)} businesses", session_id=request.session_id)

            # Create visualizations
            viz = BusinessLocationVisualization(
                location=location,
                business_type=business_type or keyword,
                radius=radius
            )

            # Create temporary directory for files
            temp_dir = self.config.temp_dir
            os.makedirs(temp_dir, exist_ok=True)

            map_path = os.path.join(temp_dir, f"{request.session_id}_map.html")
            charts_path = os.path.join(temp_dir, f"{request.session_id}_charts.png")

            # Generate visualizations
            self.log_info("Generating visualizations...", session_id=request.session_id)

            viz.create_interactive_map(businesses=businesses, save_path=map_path)
            viz.create_charts(businesses=businesses, save_path=charts_path)

            # Upload files to storage (if file_manager available)
            outputs = {}
            if hasattr(self, 'file_manager') and self.file_manager:
                try:
                    outputs['map'] = self.file_manager.save_file_to_cloud(
                        map_path,
                        f"analysis_results/{request.session_id}/map.html"
                    )
                    outputs['charts'] = self.file_manager.save_file_to_cloud(
                        charts_path,
                        f"analysis_results/{request.session_id}/charts.png"
                    )
                    # Also save charts.png as preview.png for thumbnail
                    outputs['preview'] = self.file_manager.save_file_to_cloud(
                        charts_path,
                        f"analysis_results/{request.session_id}/preview.png"
                    )
                    self.log_info("Files uploaded to cloud storage (including preview thumbnail)", session_id=request.session_id)
                except Exception as e:
                    self.log_error(f"Failed to upload files: {e}", session_id=request.session_id)
                    # Keep local paths as fallback
                    outputs['map'] = map_path
                    outputs['charts'] = charts_path
            else:
                # Use local paths
                outputs['map'] = map_path
                outputs['charts'] = charts_path

            # Calculate metadata
            metadata = {
                'business_type': business_type or keyword,
                'location_name': location.get('name', 'Unknown'),
                'radius_m': radius,
                'total_businesses': len(businesses),
                'analysis_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'businesses': businesses[:20]  # Store first 20 for metadata
            }

            processing_time = time.time() - start_time

            self.log_info(
                "Business location analysis completed",
                session_id=request.session_id,
                processing_time=f"{processing_time:.2f}s"
            )

            return self.create_result(
                request=request,
                status='completed',
                outputs=outputs,
                metadata=metadata,
                processing_time=processing_time
            )

        except ValidationException as e:
            self.log_error(f"Validation error: {e}", session_id=request.session_id)
            raise

        except Exception as e:
            self.log_error(
                f"Unexpected error during business location processing: {e}",
                session_id=request.session_id
            )
            raise ProcessorException(f"Business location processing failed: {e}")

    def _search_businesses(self, location: Dict, parameters: Dict, session_id: str) -> List[Dict]:
        """
        Search for businesses using Google Places API

        Args:
            location: Location dictionary with lat/lon
            parameters: Search parameters (business_type, keyword, radius)
            session_id: Session ID for logging

        Returns:
            List of business dictionaries
        """
        if not self.api_key:
            return self._generate_mock_data(location, parameters)

        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            center_lat = location['latitude']['center']
            center_lng = location['longitude']['center']
        else:
            center_lat = location['latitude']
            center_lng = location['longitude']

        location_name = location.get('name', '')

        # Fix business_type that might contain pipe characters
        business_type = parameters.get('business_type', '')
        if '|' in business_type:
            business_type = business_type.replace('|', ' ')

        keyword = parameters.get('keyword', '')
        radius = parameters.get('radius', 2000)

        # Build flexible text query
        text_query_parts = []

        if keyword:
            text_query_parts.append(keyword)

        if business_type:
            text_query_parts.append(business_type)

        # Add location
        if location_name:
            text_query_parts.append(f"in {location_name}")
        else:
            text_query_parts.append(f"near {center_lat},{center_lng}")

        # If no business type or keyword, use a default query
        if not keyword and not business_type:
            text_query_parts = [f"businesses near {location_name or f'{center_lat},{center_lng}'}"]

        text_query = " ".join(text_query_parts)

        self.log_info(f"Searching for: {text_query}", session_id=session_id)

        headers = {
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': self.api_key,
            'X-Goog-FieldMask': 'places.displayName,places.formattedAddress,places.location,places.rating,places.userRatingCount,places.priceLevel,places.types,places.businessStatus,places.websiteUri,places.nationalPhoneNumber'
        }

        body = {
            "textQuery": text_query,
            "locationBias": {
                "circle": {
                    "center": {
                        "latitude": center_lat,
                        "longitude": center_lng
                    },
                    "radius": radius
                }
            },
            "maxResultCount": 20,
            "rankPreference": "RELEVANCE"
        }

        try:
            response = requests.post(self.search_text_url, headers=headers, json=body)

            print(f"📡 API Response Status: {response.status_code}")
            if response.status_code != 200:
                print(f"❌ API Error Response: {response.text}")

            response.raise_for_status()
            data = response.json()

            places = data.get('places', [])
            print(f"✅ Found {len(places)} raw places from API")

            # Debug: Print first place to see the structure
            if places:
                print(f"📊 Sample place data structure: {json.dumps(places[0], indent=2)[:500]}...")

            processed = self._process_places_data(places)

            # Filter out irrelevant results
            search_term = (business_type or keyword).lower()
            if search_term:
                filtered = self._filter_relevant_places(processed, search_term)
                print(f"🔍 Relevance filter: {len(processed)} → {len(filtered)} places (search: '{search_term}')")
                return filtered if filtered else processed  # fallback to all if filter too aggressive

            return processed

        except requests.exceptions.HTTPError as e:
            self.log_error(f"Places API HTTP error: {e}", session_id=session_id)
            return self._generate_mock_data(location, parameters)
        except Exception as e:
            self.log_error(f"Places API error: {e}", session_id=session_id)
            import traceback
            traceback.print_exc()
            return self._generate_mock_data(location, parameters)

    def _process_places_data(self, places: List[Dict]) -> List[Dict]:
        """Process generic place data without assumptions about business type"""
        processed = []
        for place in places:
            try:
                # Build a generic place object that works for any business type
                place_data = {
                    'name': place.get('displayName', {}).get('text', 'Unknown'),
                    'address': place.get('formattedAddress', 'N/A'),
                    'latitude': place.get('location', {}).get('latitude', 0),
                    'longitude': place.get('location', {}).get('longitude', 0),
                    'types': place.get('types', []),
                    'business_status': place.get('businessStatus', 'OPERATIONAL'),
                    'website': place.get('websiteUri', 'N/A'),
                    'phone': place.get('nationalPhoneNumber', 'N/A')
                }

                # Add rating if available (not all places have ratings)
                if 'rating' in place:
                    place_data['rating'] = float(place.get('rating', 0))
                    place_data['rating_count'] = int(place.get('userRatingCount', 0))
                else:
                    place_data['rating'] = None
                    place_data['rating_count'] = 0

                # Handle price level - skip it to avoid errors
                place_data['price_level'] = None

                processed.append(place_data)
            except Exception as e:
                print(f"⚠️ Error processing place {place.get('displayName', {}).get('text', 'Unknown')}: {e}")
                continue
        return processed

    def _filter_relevant_places(self, places: List[Dict], search_term: str) -> List[Dict]:
        """
        Filter out places that are clearly irrelevant to the search term.
        
        Uses a combination of name matching, type matching, and exclusion of
        common false-positive categories (supermarkets, generic stores, etc.).
        """
        # Break search term into individual words for matching
        search_words = set(search_term.lower().split())
        
        # Common irrelevant place types that pollute results
        IRRELEVANT_TYPES = {
            'supermarket', 'grocery_or_supermarket', 'grocery_store',
            'convenience_store', 'department_store', 'clothing_store',
            'shoe_store', 'jewelry_store', 'electronics_store',
            'furniture_store', 'home_goods_store', 'pet_store',
            'beauty_salon', 'hair_care', 'laundry', 'car_wash',
            'car_dealer', 'car_rental', 'car_repair',
            'gas_station', 'atm', 'bank',
        }
        
        # Words in business names that indicate irrelevant generic stores
        IRRELEVANT_NAME_WORDS = {
            'migros', 'coop', 'aldi', 'lidl', 'spar', 'denner',
            'walmart', 'target', 'tesco', 'carrefour', 'edeka', 'rewe',
        }

        filtered = []
        for place in places:
            name_lower = place.get('name', '').lower()
            types = set(t.lower() for t in place.get('types', []))

            # Check if any irrelevant name word appears
            if any(word in name_lower for word in IRRELEVANT_NAME_WORDS):
                print(f"   ❌ Filtered out (irrelevant name): {place.get('name')}")
                continue

            # Check if place types are purely irrelevant (no overlap with search)
            has_irrelevant_type = bool(types & IRRELEVANT_TYPES)
            
            # Check if name or types contain any search word
            name_matches = any(w in name_lower for w in search_words)
            type_matches = any(
                any(w in t for w in search_words)
                for t in types
            )

            # Keep if name or types match the search, or if no irrelevant type
            if name_matches or type_matches or not has_irrelevant_type:
                filtered.append(place)
            else:
                print(f"   ❌ Filtered out (irrelevant type): {place.get('name')} [{', '.join(types)}]")

        return filtered

    def _generate_mock_data(self, location: Dict, parameters: Dict) -> List[Dict]:
        """Generate generic mock data for any business type"""
        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            center_lat = location['latitude']['center']
            center_lng = location['longitude']['center']
        else:
            center_lat = location['latitude']
            center_lng = location['longitude']

        business_type = parameters.get('business_type', 'business')
        keyword = parameters.get('keyword', '')

        print("⚠️  Using mock data (API key not available or API error)")

        mock_businesses = []
        for i in range(10):
            mock_business = {
                'name': f"{keyword or ''} {business_type.title()} {i+1}".strip(),
                'address': f"{i+1} Main Street, {location.get('name', 'City')}",
                'latitude': center_lat + (np.random.random() - 0.5) * 0.02,
                'longitude': center_lng + (np.random.random() - 0.5) * 0.02,
                'types': [business_type.lower().replace(' ', '_')],
                'business_status': 'OPERATIONAL',
                'website': f"https://example{i+1}.com",
                'phone': f"+1-555-{np.random.randint(100, 999)}-{np.random.randint(1000, 9999)}"
            }

            # Only add rating for businesses that typically have ratings
            if business_type.lower() in ['restaurant', 'cafe', 'hotel', 'shop', 'store']:
                mock_business['rating'] = round(3 + np.random.random() * 2, 1)
                mock_business['rating_count'] = np.random.randint(10, 500)
                mock_business['price_level'] = np.random.choice(['$', '$$', '$$$', None])
            else:
                mock_business['rating'] = None
                mock_business['rating_count'] = 0
                mock_business['price_level'] = None

            mock_businesses.append(mock_business)

        return mock_businesses

    def get_capabilities(self) -> Dict[str, Any]:
        """Return processor capabilities"""
        return {
            'analysis_type': self.get_analysis_type(),
            'name': 'Business Location Analysis',
            'description': 'Find and analyze business locations using Google Places API',
            'data_source': 'Google Places API',
            'search_types': ['keyword', 'business_type', 'category'],
            'max_radius_m': 50000,  # 50km
            'default_radius_m': 2000,  # 2km
            'max_results': 20,
            'use_cases': [
                'Business location scouting',
                'Competitor analysis',
                'Market research',
                'Store location planning'
            ]
        }
