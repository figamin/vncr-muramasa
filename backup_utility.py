#!/usr/bin/env python3
"""
Manual database backup utility script
Can be run independently of the Discord bot
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add the project root to Python path
sys.path.append(str(Path(__file__).parent))

from utils.database import Database

async def main():
    """Main backup utility function"""
    db = Database()
    
    if len(sys.argv) < 2:
        print("Usage: python backup_utility.py <command> [args]")
        print("Commands:")
        print("  create [name]     - Create a backup (optional custom name)")
        print("  list              - List all backups")
        print("  status            - Show backup system status")
        print("  restore <name>    - Restore from backup (use with caution!)")
        print("  cleanup           - Clean up old backups")
        return
    
    command = sys.argv[1].lower()
    
    try:
        if command == "create":
            backup_name = sys.argv[2] if len(sys.argv) > 2 else None
            print("Creating database backup...")
            success = await db.create_backup(backup_name)
            if success:
                print("✅ Backup created successfully!")
            else:
                print("❌ Backup creation failed!")
                
        elif command == "list":
            print("📁 Available backups:")
            backups = await db.list_backups()
            if not backups:
                print("No backups found.")
            else:
                print(f"Found {len(backups)} backup(s):")
                print("-" * 60)
                for i, backup in enumerate(backups, 1):
                    print(f"{i:2d}. {backup['name']}")
                    print(f"     Size: {backup['size_mb']} MB")
                    print(f"     Created: {backup['created']}")
                    print()
                    
        elif command == "status":
            print("📊 Backup System Status:")
            status = await db.get_backup_status()
            if status:
                print(f"Backup Count: {status.get('backup_count', 0)}")
                print(f"Max Backups: {status.get('max_backups', 'N/A')}")
                print(f"Backup Interval: {status.get('backup_interval_hours', 'N/A')} hours")
                print(f"Total Size: {status.get('total_backup_size_mb', 0)} MB")
                print(f"Backup Directory: {status.get('backup_dir', 'N/A')}")
                
                latest = status.get('latest_backup')
                if latest:
                    print(f"\nLatest Backup:")
                    print(f"  Name: {latest['name']}")
                    print(f"  Size: {latest['size_mb']} MB")
                    print(f"  Created: {latest['created']}")
                
                next_due = status.get('next_backup_due')
                if next_due:
                    print(f"\nNext Backup Due: {next_due}")
            else:
                print("Could not retrieve status information.")
                
        elif command == "restore":
            if len(sys.argv) < 3:
                print("❌ Please specify a backup name to restore.")
                return
                
            backup_name = sys.argv[2]
            print(f"⚠️  WARNING: This will replace the current database with backup '{backup_name}'")
            print("A backup of the current database will be created first.")
            confirm = input("Type 'YES' to confirm: ")
            
            if confirm == "YES":
                print("Restoring database...")
                success = await db.restore_backup(backup_name)
                if success:
                    print("✅ Database restored successfully!")
                else:
                    print("❌ Database restore failed!")
            else:
                print("❌ Restore cancelled.")
                
        elif command == "cleanup":
            print("Cleaning up old backups...")
            await db._cleanup_old_backups()
            print("✅ Cleanup completed!")
            
        else:
            print(f"❌ Unknown command: {command}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
