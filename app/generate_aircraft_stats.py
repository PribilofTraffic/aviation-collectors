#!/usr/bin/env python3
"""
Generate monthly aircraft statistics from ADS-B database
Creates /data/aircraft_stats.json for use by the homepage stats counter
"""

import os
import time

import settings
settings.apply_timezone()

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

# Database path
DB_PATH = settings.DB_PATH
OUTPUT_PATH = settings.data_path('aircraft_stats.json')

def get_monthly_aircraft_count():
    """Get unique aircraft count for the current month"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get first day of current month
        now = datetime.now()
        first_day = datetime(now.year, now.month, 1)
        
        # Query for unique aircraft this month
        query = """
        SELECT COUNT(DISTINCT hex) as unique_aircraft
        FROM aircraft_sightings
        WHERE date(timestamp) >= date(?)
        """
        
        cursor.execute(query, (first_day.strftime('%Y-%m-%d'),))
        result = cursor.fetchone()
        
        unique_aircraft = result[0] if result else 0
        
        conn.close()
        return unique_aircraft
        
    except Exception as e:
        print(f"Error querying database: {e}")
        return 0

def get_last_30_days_count():
    """Get unique aircraft count for last 30 days"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get date 30 days ago
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        query = """
        SELECT COUNT(DISTINCT hex) as unique_aircraft
        FROM aircraft_sightings
        WHERE date(timestamp) >= date(?)
        """
        
        cursor.execute(query, (thirty_days_ago.strftime('%Y-%m-%d'),))
        result = cursor.fetchone()
        
        unique_aircraft = result[0] if result else 0
        
        conn.close()
        return unique_aircraft
        
    except Exception as e:
        print(f"Error querying database: {e}")
        return 0

def main():
    """Generate aircraft statistics JSON file"""
    
    # Check if database exists
    if not Path(DB_PATH).exists():
        print(f"Database not found: {DB_PATH}")
        return
    
    # Get counts
    monthly_count = get_monthly_aircraft_count()
    last_30_days = get_last_30_days_count()
    
    # Create stats object
    stats = {
        "generated": datetime.now().isoformat(),
        "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p %Z"),
        "current_month": {
            "count": monthly_count,
            "month": datetime.now().strftime("%B %Y")
        },
        "last_30_days": {
            "count": last_30_days
        }
    }
    
    # Write to JSON file
    try:
        with open(OUTPUT_PATH, 'w') as f:
            json.dump(stats, f, indent=2)
        print(f"✅ Generated {OUTPUT_PATH}")
        print(f"   This month: {monthly_count} aircraft")
        print(f"   Last 30 days: {last_30_days} aircraft")
    except Exception as e:
        print(f"Error writing JSON file: {e}")

if __name__ == "__main__":
    main()
