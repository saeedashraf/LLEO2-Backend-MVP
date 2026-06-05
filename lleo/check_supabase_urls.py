#!/usr/bin/env python3
"""
Check what URLs are stored in Supabase for recent analyses
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.supabase_client import SupabaseClient

supabase_client = SupabaseClient()

# Get recent analyses
response = supabase_client.client.table('analyses').select('*').order('created_at', desc=True).limit(5).execute()

print(f"Found {len(response.data)} recent analyses:\n")

for i, analysis in enumerate(response.data, 1):
    print(f"=== Analysis {i} ===")
    print(f"Session ID: {analysis.get('session_id')}")
    print(f"Query: {analysis.get('query')}")
    print(f"Created: {analysis.get('created_at')}")
    print(f"Conversation ID: {analysis.get('conversation_id')}")
    print(f"\nURLs:")
    print(f"  map_url: {analysis.get('map_url')}")
    print(f"  chart_url: {analysis.get('chart_url')}")
    print(f"  charts_url: {analysis.get('charts_url')}")
    print(f"  thumbnail_url: {analysis.get('thumbnail_url')}")
    print(f"  preview_url: {analysis.get('preview_url')}")
    print()
