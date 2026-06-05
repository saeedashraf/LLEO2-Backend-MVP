"""
Time Period Extraction using Vertex AI Gemini
Handles sophisticated time period parsing including years, months, and seasons
"""

import re


async def extract_time_period(query: str, model) -> tuple[int, int, list]:
    """
    Extract sophisticated time period including years and specific months from query.

    Args:
        query: User's natural language query
        model: Vertex AI Gemini model instance

    Returns:
        tuple: (start_year, end_year, specific_months)
        specific_months: List of month numbers (1-12) or empty list for all months

    Handles complex scenarios:
    - Single year: "2020" → (2020, 2020, [])
    - Year range: "2020 to 2023" → (2020, 2023, [])
    - Specific month+year: "July 2020" → (2020, 2020, [7])
    - Month range same year: "July to August 2020" → (2020, 2020, [7,8])
    - Month range across years: "December 2020 to February 2021" → (2020, 2021, [12,1,2])
    - Season: "summer 2020" → (2020, 2020, [6,7,8])
    - Compare two months: "July 2020 vs July 2023" → (2020, 2023, [7])
    - Compare two years: "2020 vs 2023" → (2020, 2023, [])
    """
    print(f"📅 Extracting sophisticated time period from query")

    prompt = f"""
    CAREFULLY analyze this query and extract BOTH the year range AND specific months if mentioned:

    Query: "{query}"

    YOUR TASK: Understand the EXACT time period the user wants to analyze.

    SCENARIOS TO HANDLE:

    1. SINGLE YEAR, ALL MONTHS:
       "vegetation 2020" → Start Year: 2020, End Year: 2020, Specific Months: all

    2. YEAR RANGE, ALL MONTHS (Comparison):
       "vegetation 2020 2023" → Start Year: 2020, End Year: 2023, Specific Months: all
       "compare 2020 to 2023" → Start Year: 2020, End Year: 2023, Specific Months: all

    3. SPECIFIC MONTH(S) IN ONE YEAR:
       "July 2020" → Start Year: 2020, End Year: 2020, Specific Months: 7
       "July and August 2020" → Start Year: 2020, End Year: 2020, Specific Months: 7,8

    4. SAME MONTH(S) ACROSS MULTIPLE YEARS (Comparison):
       "July 2020 vs July 2023" → Start Year: 2020, End Year: 2023, Specific Months: 7
       "summer 2020 compared to summer 2023" → Start Year: 2020, End Year: 2023, Specific Months: 6,7,8

    5. MONTH RANGE WITHIN SAME YEAR:
       "May to September 2020" → Start Year: 2020, End Year: 2020, Specific Months: 5,6,7,8,9

    6. MONTH RANGE ACROSS YEARS:
       "December 2020 to March 2021" → Start Year: 2020, End Year: 2021, Specific Months: 12,1,2,3

    7. SEASONAL REFERENCES:
       "summer 2020" → Start Year: 2020, End Year: 2020, Specific Months: 6,7,8
       "winter" → Start Year: 2023, End Year: 2024, Specific Months: 12,1,2
       "spring to fall 2020" → Start Year: 2020, End Year: 2020, Specific Months: 3,4,5,6,7,8,9,10,11

    8. NO SPECIFIC TIME (Default):
       "vegetation analysis" → Start Year: 2020, End Year: 2023, Specific Months: all

    SEASONAL MAPPINGS:
    - "summer" or "summer months" → 6,7,8 (Jun, Jul, Aug)
    - "winter" or "winter months" → 12,1,2 (Dec, Jan, Feb)
    - "spring" or "spring months" → 3,4,5 (Mar, Apr, May)
    - "fall" or "autumn" → 9,10,11 (Sep, Oct, Nov)

    IMPORTANT RULES:
    1. If user compares same month across years (e.g., "July 2020 vs July 2023"), specify that month
    2. If user wants full year comparison (e.g., "2020 vs 2023"), use "all"
    3. If month range spans years, include ALL months in the range
    4. Years must be between 2015-2025
    5. If no years mentioned, default to 2020-2023

    RESPOND IN THIS EXACT FORMAT:
    Start Year: [year]
    End Year: [year]
    Specific Months: [comma-separated month numbers like "6,7,8" OR "all"]
    Reasoning: [detailed explanation of what time period you extracted and WHY]

    EXAMPLE RESPONSES:

    Query: "July 2020"
    Start Year: 2020
    End Year: 2020
    Specific Months: 7
    Reasoning: Single month (July) in single year (2020)

    Query: "summer vegetation 2020 compared to 2023"
    Start Year: 2020
    End Year: 2023
    Specific Months: 6,7,8
    Reasoning: Comparing summer months (Jun, Jul, Aug) between 2020 and 2023

    Query: "December 2019 to February 2020"
    Start Year: 2019
    End Year: 2020
    Specific Months: 12,1,2
    Reasoning: Winter months spanning from Dec 2019 to Feb 2020

    Query: "vegetation changes 2020 to 2024"
    Start Year: 2020
    End Year: 2024
    Specific Months: all
    Reasoning: Full year comparison across 5 years, no specific months mentioned
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 300,
            "temperature": 0.1
        })

        text = response.text.strip()
        print(f"📆 Time period response: {text}")

        start_year = extract_numeric_field(text, "Start Year")
        end_year = extract_numeric_field(text, "End Year")

        # Extract specific months
        specific_months = []
        months_match = re.search(r'Specific Months:\s*(.+)', text, re.IGNORECASE)
        if months_match:
            months_str = months_match.group(1).strip().split(',')[0].strip()  # Get first part before reasoning
            if months_str.lower() not in ['all', 'none', 'n/a']:
                # Parse comma-separated month numbers
                month_parts = re.findall(r'\d+', months_str)
                specific_months = [int(m) for m in month_parts if 1 <= int(m) <= 12]
                # Remove duplicates and sort
                specific_months = sorted(list(set(specific_months)))

        if not start_year or not end_year:
            years = re.findall(r'\b(20\d\d)\b', query)
            if len(years) >= 2:
                start_year = int(years[0])
                end_year = int(years[-1])
            else:
                start_year, end_year = 2020, 2023

        start_year = max(2015, int(start_year))
        end_year = min(2025, int(end_year))

        if start_year >= end_year:
            end_year = start_year + 3
            end_year = min(2025, end_year)

        if specific_months:
            month_names = {1:'Jan', 2:'Feb', 3:'Mar', 4:'Apr', 5:'May', 6:'Jun',
                          7:'Jul', 8:'Aug', 9:'Sep', 10:'Oct', 11:'Nov', 12:'Dec'}
            month_str = ','.join([month_names[m] for m in specific_months])
            print(f"✅ Time period: {start_year} to {end_year}, Specific months: {month_str} ({specific_months})")
        else:
            print(f"✅ Time period: {start_year} to {end_year}, All months (year-round)")

        return start_year, end_year, specific_months

    except Exception as e:
        print(f"❌ Time period extraction failed: {e}")
        return 2020, 2023, []


def extract_numeric_field(text: str, field_name: str) -> float:
    """Extract numeric value from text field"""
    pattern = rf"{field_name}:\s*([-+]?\d*\.?\d+)"
    match = re.search(pattern, text, re.IGNORECASE)
    return float(match.group(1)) if match else None
