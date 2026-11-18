#!/usr/bin/env python3
"""
ADS-B history collector
Collects aircraft data from tar1090/readsb and stores in SQLite database
"""

import requests
import sqlite3
import json
from datetime import datetime
import os
import sys
import time

import settings

settings.apply_timezone()

# Configuration
AIRCRAFT_JSON_URL = settings.TAR1090_URL
DATABASE_PATH = settings.DB_PATH
COLLECTION_INTERVAL = settings.get_int('AVIATION_ADSB_INTERVAL', 60)  # seconds

def initialize_database():
    """Create database tables if they don't exist"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Aircraft sightings table - every time we see an aircraft
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS aircraft_sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            hex TEXT NOT NULL,
            flight TEXT,
            alt_baro INTEGER,
            alt_geom INTEGER,
            gs REAL,
            track INTEGER,
            baro_rate INTEGER,
            category TEXT,
            lat REAL,
            lon REAL,
            rssi REAL,
            messages INTEGER,
            seen REAL,
            seen_pos REAL
        )
    ''')
    
    # Unique aircraft table - one record per unique aircraft
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS unique_aircraft (
            hex TEXT PRIMARY KEY,
            registration TEXT,
            aircraft_type TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            total_sightings INTEGER DEFAULT 1,
            max_altitude INTEGER,
            max_speed REAL,
            flight_callsigns TEXT
        )
    ''')
    
    # Daily statistics table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            total_aircraft INTEGER,
            unique_aircraft INTEGER,
            total_messages INTEGER,
            avg_altitude REAL,
            max_altitude INTEGER,
            avg_speed REAL,
            max_speed REAL
        )
    ''')
    
    # Create indexes for better query performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sightings_timestamp ON aircraft_sightings(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sightings_hex ON aircraft_sightings(hex)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_unique_last_seen ON unique_aircraft(last_seen)')
    
    conn.commit()
    conn.close()
    
    print(f"✓ Database initialized: {DATABASE_PATH}")


def fetch_aircraft_data():
    """Fetch current aircraft data from tar1090"""
    try:
        response = requests.get(AIRCRAFT_JSON_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('aircraft', [])
    except Exception as e:
        print(f"Error fetching aircraft data: {e}")
        return []


def store_aircraft_sightings(aircraft_list):
    """Store aircraft sightings in database"""
    if not aircraft_list:
        return 0
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    timestamp = datetime.now().isoformat()
    
    stored_count = 0
    
    for aircraft in aircraft_list:
        # Only store aircraft with valid hex
        if 'hex' not in aircraft:
            continue
        
        try:
            cursor.execute('''
                INSERT INTO aircraft_sightings (
                    timestamp, hex, flight, alt_baro, alt_geom, gs, track,
                    baro_rate, category, lat, lon, rssi, messages, seen, seen_pos
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                timestamp,
                aircraft.get('hex'),
                aircraft.get('flight', '').strip() if aircraft.get('flight') else None,
                aircraft.get('alt_baro'),
                aircraft.get('alt_geom'),
                aircraft.get('gs'),
                aircraft.get('track'),
                aircraft.get('baro_rate'),
                aircraft.get('category'),
                aircraft.get('lat'),
                aircraft.get('lon'),
                aircraft.get('rssi'),
                aircraft.get('messages'),
                aircraft.get('seen'),
                aircraft.get('seen_pos')
            ))
            
            # Update unique aircraft table
            hex_code = aircraft.get('hex')
            flight = aircraft.get('flight', '').strip() if aircraft.get('flight') else None
            alt_baro = aircraft.get('alt_baro')
            gs = aircraft.get('gs')
            
            # Check if aircraft exists
            cursor.execute('SELECT hex, flight_callsigns, max_altitude, max_speed FROM unique_aircraft WHERE hex = ?', (hex_code,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing aircraft
                old_callsigns = existing[1] or ''
                callsigns_list = old_callsigns.split(',') if old_callsigns else []
                
                if flight and flight not in callsigns_list:
                    callsigns_list.append(flight)
                    new_callsigns = ','.join(callsigns_list[:10])  # Keep last 10 callsigns
                else:
                    new_callsigns = old_callsigns
                
                new_max_alt = max(existing[2] or 0, alt_baro or 0)
                new_max_speed = max(existing[3] or 0, gs or 0)
                
                cursor.execute('''
                    UPDATE unique_aircraft 
                    SET last_seen = ?,
                        total_sightings = total_sightings + 1,
                        max_altitude = ?,
                        max_speed = ?,
                        flight_callsigns = ?
                    WHERE hex = ?
                ''', (timestamp, new_max_alt, new_max_speed, new_callsigns, hex_code))
            else:
                # Insert new aircraft
                cursor.execute('''
                    INSERT INTO unique_aircraft (
                        hex, first_seen, last_seen, max_altitude, max_speed, flight_callsigns
                    ) VALUES (?, ?, ?, ?, ?, ?)
                ''', (hex_code, timestamp, timestamp, alt_baro, gs, flight))
            
            stored_count += 1
            
        except Exception as e:
            print(f"Error storing aircraft {aircraft.get('hex')}: {e}")
            continue
    
    conn.commit()
    conn.close()
    
    return stored_count


def update_daily_stats():
    """Update daily statistics"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    today = datetime.now().date().isoformat()
    
    # Calculate today's stats
    cursor.execute('''
        SELECT 
            COUNT(*) as total_sightings,
            COUNT(DISTINCT hex) as unique_aircraft,
            SUM(messages) as total_messages,
            AVG(alt_baro) as avg_altitude,
            MAX(alt_baro) as max_altitude,
            AVG(gs) as avg_speed,
            MAX(gs) as max_speed
        FROM aircraft_sightings
        WHERE DATE(timestamp) = ?
    ''', (today,))
    
    stats = cursor.fetchone()
    
    if stats:
        cursor.execute('''
            INSERT OR REPLACE INTO daily_stats (
                date, total_aircraft, unique_aircraft, total_messages,
                avg_altitude, max_altitude, avg_speed, max_speed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (today, stats[0], stats[1], stats[2] or 0, stats[3], stats[4], stats[5], stats[6]))
        
        conn.commit()
    
    conn.close()


def collect_once(count=1):
    """One poll of the receiver. Returns the number of records stored."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    aircraft_list = fetch_aircraft_data()
    if not aircraft_list:
        print(f"[{timestamp}] No aircraft data received")
        return 0
    stored = store_aircraft_sightings(aircraft_list)
    print(f"[{timestamp}] Collected {len(aircraft_list)} aircraft, stored {stored} records")
    # Daily stats are cheap to refresh but not needed every minute.
    if count % 10 == 0:
        update_daily_stats()
    return stored


def main():
    """Collect forever, or once with --once (the container's scheduler calls
    collect_once() itself)."""
    if not AIRCRAFT_JSON_URL:
        print('AVIATION_TAR1090_URL is not set; nothing to collect from')
        return 1
    os.makedirs(os.path.dirname(DATABASE_PATH) or '.', exist_ok=True)
    initialize_database()
    if '--once' in sys.argv:
        collect_once(10)
        return 0

    print(f"Collecting from: {AIRCRAFT_JSON_URL}")
    print(f"Database: {DATABASE_PATH}")
    print(f"Interval: {COLLECTION_INTERVAL} seconds")
    count = 0
    try:
        while True:
            count += 1
            collect_once(count)
            time.sleep(COLLECTION_INTERVAL)
    except KeyboardInterrupt:
        update_daily_stats()
        print("Collector stopped")
    return 0


if __name__ == '__main__':
    sys.exit(main())
