"""
Conversation Manager for Interactive Clarification

Manages multi-turn conversations with users to gather missing information
for satellite imagery analysis requests.

Now uses Supabase for persistent storage across Cloud Run instances.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import os


class ConversationState:
    """Represents the state of a conversation with a user"""

    def __init__(self, session_id: str, user_id: str, initial_query: str):
        self.session_id = session_id
        self.conversation_id = session_id  # conversation_id is same as session_id
        self.user_id = user_id
        self.initial_query = initial_query
        self.created_at = datetime.now().isoformat()
        self.messages: List[Dict[str, str]] = []

        # Extracted information
        self.analysis_type: Optional[str] = None
        self.location: Optional[str] = None
        self.start_year: Optional[int] = None
        self.end_year: Optional[int] = None
        self.specific_months: Optional[List[int]] = None
        self.parameters: Dict[str, Any] = {}  # For parameter-specific requests

        # Status
        self.is_complete: bool = False
        self.needs_clarification: List[str] = []  # List of what needs clarification

    def add_message(self, role: str, content: str):
        """Add a message to conversation history"""
        self.messages.append({
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        return {
            'session_id': self.session_id,
            'conversation_id': self.conversation_id,
            'user_id': self.user_id,
            'initial_query': self.initial_query,
            'created_at': self.created_at,
            'messages': self.messages,
            'analysis_type': self.analysis_type,
            'location': self.location,
            'start_year': self.start_year,
            'end_year': self.end_year,
            'specific_months': self.specific_months,
            'parameters': self.parameters,
            'is_complete': self.is_complete,
            'needs_clarification': self.needs_clarification
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ConversationState':
        """Create from dictionary"""
        state = cls(
            session_id=data['session_id'],
            user_id=data['user_id'],
            initial_query=data['initial_query']
        )
        # conversation_id is already set in __init__, but restore if present in data
        if 'conversation_id' in data:
            state.conversation_id = data['conversation_id']
        state.created_at = data['created_at']
        state.messages = data['messages']
        state.analysis_type = data.get('analysis_type')
        state.location = data.get('location')
        state.start_year = data.get('start_year')
        state.end_year = data.get('end_year')
        state.specific_months = data.get('specific_months')
        state.parameters = data.get('parameters', {})
        state.is_complete = data.get('is_complete', False)
        state.needs_clarification = data.get('needs_clarification', [])
        return state


class ConversationManager:
    """
    Manages conversations with Supabase persistence

    Stores conversation state in Supabase to maintain sessions across
    multiple Cloud Run instances. Falls back to in-memory if Supabase unavailable.
    """

    def __init__(self, supabase_client=None):
        self.supabase = supabase_client
        self._local_cache: Dict[str, ConversationState] = {}

        if self.supabase is None:
            try:
                from ..supabase_client import supabase_client as sb_client
                self.supabase = sb_client
                print("✅ ConversationManager using Supabase persistence")
            except Exception as e:
                print(f"⚠️  Supabase not available: {e}")
                print("⚠️  Using in-memory storage (sessions won't persist)")

    def create_conversation(self, session_id: str, user_id: str, query: str) -> ConversationState:
        """Create conversation and save to Supabase"""
        state = ConversationState(session_id, user_id, query)

        if self.supabase:
            try:
                data = {
                    'session_id': state.session_id,
                    'user_id': state.user_id,
                    'initial_query': state.initial_query,
                    'conversation_data': state.to_dict(),
                    'analysis_type': state.analysis_type,
                    'location': state.location,
                    'start_year': state.start_year,
                    'end_year': state.end_year,
                    'specific_months': state.specific_months,
                    'parameters': state.parameters,
                    'is_complete': state.is_complete,
                    'needs_clarification': state.needs_clarification
                }
                self.supabase.client.table('conversations').insert(data).execute()
                print(f"💾 Saved conversation {session_id} to Supabase")
            except Exception as e:
                print(f"⚠️  Failed to save to Supabase: {e}")

        self._local_cache[session_id] = state
        return state

    def get_conversation(self, session_id: str) -> Optional[ConversationState]:
        """Get conversation from Supabase or cache"""
        if session_id in self._local_cache:
            return self._local_cache[session_id]

        if self.supabase:
            try:
                response = self.supabase.client.table('conversations').select('*').eq('session_id', session_id).single().execute()
                if response.data:
                    conversation_data = response.data.get('conversation_data', {})
                    state = ConversationState.from_dict(conversation_data)
                    self._local_cache[session_id] = state
                    print(f"📥 Loaded conversation {session_id} from Supabase")
                    return state
            except Exception as e:
                if 'No rows found' not in str(e) and 'PGRST116' not in str(e):
                    print(f"⚠️  Failed to load from Supabase: {e}")
        return None

    def update_conversation(self, session_id: str, state: ConversationState):
        """Update conversation in Supabase"""
        self._local_cache[session_id] = state

        if self.supabase:
            try:
                data = {
                    'conversation_data': state.to_dict(),
                    'analysis_type': state.analysis_type,
                    'location': state.location,
                    'start_year': state.start_year,
                    'end_year': state.end_year,
                    'specific_months': state.specific_months,
                    'parameters': state.parameters,
                    'is_complete': state.is_complete,
                    'needs_clarification': state.needs_clarification
                }
                self.supabase.client.table('conversations').update(data).eq('session_id', session_id).execute()
                print(f"💾 Updated conversation {session_id}")
            except Exception as e:
                print(f"⚠️  Failed to update Supabase: {e}")

    def delete_conversation(self, session_id: str):
        """Delete conversation from Supabase"""
        if session_id in self._local_cache:
            del self._local_cache[session_id]

        if self.supabase:
            try:
                self.supabase.client.table('conversations').delete().eq('session_id', session_id).execute()
                print(f"🗑️  Deleted conversation {session_id}")
            except Exception as e:
                print(f"⚠️  Failed to delete from Supabase: {e}")


# Global conversation manager instance
conversation_manager = ConversationManager()


# Parameter options for different analysis types
ANALYSIS_PARAMETERS = {
    'water_quality': {
        'name': 'Water Quality',
        'parameters': [
            {'key': 'ndwi', 'name': 'NDWI (Water Index)', 'description': 'Normalized Difference Water Index - measures water presence'},
            {'key': 'turbidity', 'name': 'Turbidity', 'description': 'Water clarity and sediment content'},
            {'key': 'chlorophyll', 'name': 'Chlorophyll', 'description': 'Algae and phytoplankton concentration'}
        ],
        'default': 'all'
    },
    'air_quality': {
        'name': 'Air Quality',
        'parameters': [
            {'key': 'no2', 'name': 'NO2', 'description': 'Nitrogen Dioxide - traffic and industrial pollution'},
            {'key': 'co', 'name': 'CO', 'description': 'Carbon Monoxide - combustion byproduct'},
            {'key': 'so2', 'name': 'SO2', 'description': 'Sulfur Dioxide - industrial emissions'},
            {'key': 'aerosol', 'name': 'Aerosol', 'description': 'Particulate matter in atmosphere'}
        ],
        'default': 'all'
    },
    'vegetation_index': {
        'name': 'Vegetation Index',
        'parameters': [
            {'key': 'ndvi', 'name': 'NDVI', 'description': 'Normalized Difference Vegetation Index - vegetation health'}
        ],
        'default': 'all'
    }
}


def get_parameter_options(analysis_type: str) -> Optional[Dict[str, Any]]:
    """Get parameter options for an analysis type"""
    return ANALYSIS_PARAMETERS.get(analysis_type)


def parse_parameter_selection(user_input: str, analysis_type: str) -> List[str]:
    """
    Parse user's parameter selection

    Args:
        user_input: User's response (e.g., "all", "only chlorophyll", "NDWI and turbidity")
        analysis_type: Type of analysis

    Returns:
        List of selected parameter keys
    """
    user_input_lower = user_input.lower().strip()

    param_config = ANALYSIS_PARAMETERS.get(analysis_type)
    if not param_config:
        return []

    all_params = [p['key'] for p in param_config['parameters']]

    # Check for "all" or "everything"
    if any(word in user_input_lower for word in ['all', 'everything', 'every', 'complete', 'full']):
        return all_params

    # Parse specific parameters
    selected = []
    for param in param_config['parameters']:
        param_key = param['key'].lower()
        param_name = param['name'].lower()

        # Check if parameter is mentioned
        if param_key in user_input_lower or param_name in user_input_lower:
            selected.append(param['key'])

    # If nothing selected, return all (default behavior)
    if not selected:
        return all_params

    return selected


def format_parameter_question(analysis_type: str) -> str:
    """
    Format a question asking which parameters to analyze

    Args:
        analysis_type: Type of analysis

    Returns:
        Formatted question string
    """
    param_config = ANALYSIS_PARAMETERS.get(analysis_type)
    if not param_config:
        return ""

    params_list = []
    for i, param in enumerate(param_config['parameters'], 1):
        params_list.append(f"{i}. **{param['name']}**: {param['description']}")

    params_text = '\n'.join(params_list)

    question = f"""I can analyze **{param_config['name']}** using the following indices:

{params_text}

Would you like me to analyze:
- **All** of them (comprehensive analysis)
- **Specific** ones (just tell me which ones, e.g., "only {param_config['parameters'][0]['name']}" or "{param_config['parameters'][0]['name']} and {param_config['parameters'][1]['name'] if len(param_config['parameters']) > 1 else ''}")

What would you prefer?"""

    return question
