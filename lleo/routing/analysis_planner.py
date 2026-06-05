"""
Intelligent Analysis Planner

Uses Vertex AI to decide the best analysis strategy based on user query
"""

import json
import re
from typing import Dict, Any, List, Optional


async def plan_analysis_strategy(
    query: str,
    analysis_type: str,
    location: str,
    time_period: Dict[str, Any],
    model
) -> Dict[str, Any]:
    """
    Use Vertex AI to intelligently plan the analysis strategy

    Args:
        query: User's original query
        analysis_type: Type of analysis (water_quality, vegetation_index, etc.)
        location: Location name
        time_period: Basic time period info
        model: Vertex AI Gemini model

    Returns:
        Detailed analysis plan with time periods, comparisons, and visualizations
    """
    if not model:
        # Fallback to simple plan
        return _create_simple_plan(time_period)

    prompt = f"""
    **Role:** You are an intelligent Earth observation analysis planner. Your job is to design the BEST analysis strategy based on what the user actually wants.

    **User Query:** "{query}"
    **Analysis Type:** {analysis_type}
    **Location:** {location}
    **Basic Time Info:** Start Year: {time_period.get('start_year')}, End Year: {time_period.get('end_year')}, Start Month: {time_period.get('start_month', 'not specified')}, End Month: {time_period.get('end_month', 'not specified')}

    **CRITICAL — Month constraints:**
    If Start Month and End Month are provided, you MUST limit your time_periods
    to ONLY the months in that range.  For example Start Year 2024, End Year 2025,
    Start Month 11, End Month 3 means Nov 2024, Dec 2024, Jan 2025, Feb 2025, Mar 2025
    — do NOT include any other months.

    **Your Task:** Design the optimal analysis strategy by deciding:

    1. **Time Periods to Analyze:**
       - Should we analyze specific months? (e.g., "January vs December" → analyze Jan and Dec separately)
       - Should we analyze seasons? (e.g., "winter vs summer" → Dec-Feb vs Jun-Aug)
       - Should we analyze full years? (e.g., "2020 vs 2024" → full year composites)
       - Should we analyze specific date ranges? (e.g., "first half vs second half of 2023")

    2. **Comparison Strategy:**
       - Should we compare two periods? (e.g., before/after, month1 vs month2)
       - Should we show a time series? (e.g., monthly progression)
       - Should we show single period analysis? (e.g., just current state)

    3. **Visualization Strategy:**
       - Should we show difference maps? (e.g., changes between periods)
       - Should we highlight specific indices? (e.g., focus on turbidity)
       - Should we show time series charts?
       - Should we show side-by-side comparisons?

    **Examples:**

    Query: "water quality Lake Zug in 2023 compare January and December"
    Strategy:
    {{
        "strategy_type": "month_comparison",
        "time_periods": [
            {{"year": 2023, "month": 1, "label": "January 2023", "date_range": {{"start": "2023-01-01", "end": "2023-01-31"}}}},
            {{"year": 2023, "month": 12, "label": "December 2023", "date_range": {{"start": "2023-12-01", "end": "2023-12-31"}}}}
        ],
        "comparison": {{
            "type": "two_period",
            "baseline": "January 2023",
            "comparison": "December 2023",
            "show_difference": true
        }},
        "visualization": {{
            "show_difference_maps": true,
            "show_side_by_side": true,
            "show_change_charts": true,
            "highlight_changes": true
        }},
        "reasoning": "User wants to compare two specific months within the same year. We should analyze January and December separately, calculate differences, and visualize changes between these two periods."
    }}

    Query: "deforestation in Amazon from 2020 to 2024"
    Strategy:
    {{
        "strategy_type": "year_comparison",
        "time_periods": [
            {{"year": 2020, "label": "2020", "date_range": {{"start": "2020-01-01", "end": "2020-12-31"}}}},
            {{"year": 2024, "label": "2024", "date_range": {{"start": "2024-01-01", "end": "2024-12-31"}}}}
        ],
        "comparison": {{
            "type": "two_period",
            "baseline": "2020",
            "comparison": "2024",
            "show_difference": true
        }},
        "visualization": {{
            "show_difference_maps": true,
            "show_side_by_side": true,
            "show_change_charts": true,
            "show_vegetation_loss": true
        }},
        "reasoning": "User wants to see deforestation over 4 years. We should compare full year composites from 2020 and 2024 to show vegetation loss."
    }}

    Query: "water quality in Lake Geneva summer 2023"
    Strategy:
    {{
        "strategy_type": "seasonal_analysis",
        "time_periods": [
            {{"year": 2023, "season": "summer", "label": "Summer 2023", "date_range": {{"start": "2023-06-01", "end": "2023-08-31"}}}}
        ],
        "comparison": {{
            "type": "single_period",
            "show_difference": false
        }},
        "visualization": {{
            "show_difference_maps": false,
            "show_side_by_side": false,
            "show_current_state": true,
            "show_distribution_charts": true
        }},
        "reasoning": "User wants current state for a specific season. No comparison needed, just analyze summer months."
    }}

    Query: "vegetation changes every month in 2023"
    Strategy:
    {{
        "strategy_type": "time_series",
        "time_periods": [
            {{"year": 2023, "month": 1, "label": "Jan 2023", "date_range": {{"start": "2023-01-01", "end": "2023-01-31"}}}},
            {{"year": 2023, "month": 2, "label": "Feb 2023", "date_range": {{"start": "2023-02-01", "end": "2023-02-28"}}}},
            {{"year": 2023, "month": 3, "label": "Mar 2023", "date_range": {{"start": "2023-03-01", "end": "2023-03-31"}}}},
            ...and so on for all 12 months
        ],
        "comparison": {{
            "type": "time_series",
            "show_difference": false,
            "show_trends": true
        }},
        "visualization": {{
            "show_time_series_charts": true,
            "show_monthly_progression": true,
            "show_trend_analysis": true
        }},
        "reasoning": "User wants to see monthly progression throughout the year. We should analyze each month separately and show trends."
    }}

    **IMPORTANT:**
    - Return ONLY valid JSON
    - Be specific about date ranges (use YYYY-MM-DD format)
    - Always include "reasoning" to explain your strategy
    - For month comparisons, specify exact months (1-12)
    - For seasonal analysis, define the season's date range
    - Consider what the user ACTUALLY wants to see

    **Now plan the strategy for the given query:**
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 8192,
            "temperature": 0.3
        })

        text = response.text.strip()
        print(f"🎯 Analysis Strategy Response:")
        print(text[:500] + "..." if len(text) > 500 else text)

        # Extract JSON
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            json_text = json_match.group(0)
            strategy = json.loads(json_text)

            print(f"\n📋 ANALYSIS STRATEGY:")
            print(f"   Type: {strategy.get('strategy_type')}")
            print(f"   Time Periods: {len(strategy.get('time_periods', []))}")
            print(f"   Comparison: {strategy.get('comparison', {}).get('type')}")
            print(f"   Reasoning: {strategy.get('reasoning')}")

            return strategy

    except Exception as e:
        print(f"❌ Analysis planning failed: {e}")
        import traceback
        traceback.print_exc()

    # Fallback
    return _create_simple_plan(time_period)


def _create_simple_plan(time_period: Dict[str, Any]) -> Dict[str, Any]:
    """Create a simple fallback plan"""
    start_year = time_period.get('start_year')
    end_year = time_period.get('end_year')

    if start_year == end_year:
        return {
            "strategy_type": "single_year",
            "time_periods": [
                {
                    "year": start_year,
                    "label": str(start_year),
                    "date_range": {
                        "start": f"{start_year}-01-01",
                        "end": f"{start_year}-12-31"
                    }
                }
            ],
            "comparison": {
                "type": "single_period",
                "show_difference": False
            },
            "visualization": {
                "show_current_state": True
            },
            "reasoning": "Simple single year analysis (fallback)"
        }
    else:
        return {
            "strategy_type": "year_comparison",
            "time_periods": [
                {
                    "year": start_year,
                    "label": str(start_year),
                    "date_range": {
                        "start": f"{start_year}-01-01",
                        "end": f"{start_year}-12-31"
                    }
                },
                {
                    "year": end_year,
                    "label": str(end_year),
                    "date_range": {
                        "start": f"{end_year}-01-01",
                        "end": f"{end_year}-12-31"
                    }
                }
            ],
            "comparison": {
                "type": "two_period",
                "baseline": str(start_year),
                "comparison": str(end_year),
                "show_difference": True
            },
            "visualization": {
                "show_difference_maps": True,
                "show_side_by_side": True
            },
            "reasoning": "Simple year comparison (fallback)"
        }


def extract_month_from_label(label: str) -> Optional[int]:
    """Extract month number from a label like 'January 2023' or 'Jan 2023'"""
    month_map = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }

    label_lower = label.lower()
    for month_name, month_num in month_map.items():
        if month_name in label_lower:
            return month_num

    return None
