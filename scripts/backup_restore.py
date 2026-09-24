"""
scripts/backup_restore.py — Standalone CLI utility for Remote Desktop backups & restorations.

Usage:
  python scripts/backup_restore.py backup [--dir backups] [--uploads uploads]
  python scripts/backup_restore.py verify <backup_zip_path>
  python scripts/backup_restore.py restore <backup_zip_path> [--db rd_app.db] [--uploads uploads]
"""
import argparse
import sys
import os

# Add parent directory to sys.path so app imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.backup import create_backup, verify_backup, restore_backup


def main():
    parser = argparse.ArgumentParser(description="Remote Desktop Backup & Recovery CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Backup command
    backup_parser = subparsers.add_parser("backup", help="Create a backup archive")
    backup_parser.add_argument("--dir", default="backups", help="Destination backup directory")
    backup_parser.add_argument("--db", default=None, help="Explicit SQLite DB file path")
    backup_parser.add_argument("--uploads", default="uploads", help="Uploads directory path")

    # Verify command
    verify_parser = subparsers.add_parser("verify", help="Verify backup integrity and manifest")
    verify_parser.add_argument("archive", help="Path to backup zip archive")

    # Restore command
    restore_parser = subparsers.add_parser("restore", help="Restore database and uploads from backup")
    restore_parser.add_argument("archive", help="Path to backup zip archive")
    restore_parser.add_argument("--db", default=None, help="Target SQLite DB file path")
    restore_parser.add_argument("--uploads", default="uploads", help="Target uploads directory path")

    args = parser.parse_args()

    if args.command == "backup":
        print(f"[*] Creating backup in '{args.dir}'...")
        path = create_backup(backup_dir=args.dir, db_path=args.db, uploads_dir=args.uploads)
        print(f"[+] Backup successfully created: {path}")

    elif args.command == "verify":
        print(f"[*] Verifying backup '{args.archive}'...")
        res = verify_backup(args.archive)
        if res["valid"]:
            print(f"[+] Backup valid! Files: {res['file_count']}, Has DB: {res['has_database']}")
            print(f"    Created at: {res['manifest'].get('backup_timestamp')}")
        else:
            print(f"[-] Verification failed: {res.get('error')}")
            sys.exit(1)

    elif args.command == "restore":
        print(f"[*] Restoring from backup '{args.archive}'...")
        try:
            res = restore_backup(args.archive, target_db_path=args.db, target_uploads_dir=args.uploads)
            print(f"[+] Restore successful! Restored at: {res['restored_timestamp']}")
        except Exception as exc:
            print(f"[-] Restore failed: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    main()
