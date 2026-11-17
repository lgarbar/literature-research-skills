#!/usr/bin/env python3
"""Interactive CLI tool to select active university for library routing and proxy authentication."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
UNIVERSITIES_FILE = ROOT / "config" / "universities.json"
ACTIVE_CONFIG_FILE = ROOT / "config" / "active_institution.json"
ENV_FILE = ROOT / ".env"


def load_universities():
    if not UNIVERSITIES_FILE.is_file():
        raise FileNotFoundError(f"Universities database not found at {UNIVERSITIES_FILE}")
    with open(UNIVERSITIES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_active_institution():
    if ACTIVE_CONFIG_FILE.is_file():
        try:
            with open(ACTIVE_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_active_institution(institution):
    """Save selected institution to config/active_institution.json and update .env."""
    ACTIVE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    record = dict(institution)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    # 1. Write config/active_institution.json
    with open(ACTIVE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
        
    # 2. Update .env file
    env_vars = {
        "ACTIVE_INSTITUTION_ID": record.get("id", ""),
        "ACTIVE_INSTITUTION_NAME": f'"{record.get("name", "")}"',
        "ACTIVE_INSTITUTION_PROXY_PREFIX": f'"{record.get("ezproxy_prefix", "")}"',
        "ACTIVE_INSTITUTION_LIBRARY_PORTAL": f'"{record.get("library_portal", "")}"',
        "ACTIVE_INSTITUTION_LOGIN_PORTAL": f'"{record.get("login_portal", "")}"',
    }
    
    lines = []
    if ENV_FILE.is_file():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        
    new_lines = []
    keys_handled = set()
    for line in lines:
        matched_key = None
        for k in env_vars:
            if re.match(rf"^{k}\s*=", line):
                matched_key = k
                break
        if matched_key:
            new_lines.append(f"{matched_key}={env_vars[matched_key]}")
            keys_handled.add(matched_key)
        else:
            new_lines.append(line)
            
    for k, v in env_vars.items():
        if k not in keys_handled:
            new_lines.append(f"{k}={v}")
            
    ENV_FILE.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")
    return record


def filter_universities(universities, query):
    if not query:
        return universities
    q = query.lower().strip()
    return [
        u for u in universities
        if q in u["id"].lower()
        or q in u["name"].lower()
        or q in u.get("country", "").lower()
    ]


def interactive_selection(universities):
    print("=" * 65)
    print(" 🏛️  University Library & EZproxy Setup")
    print("=" * 65)
    
    current = get_active_institution()
    if current:
        print(f"Current active institution: {current.get('name')} [{current.get('id')}]")
        print("-" * 65)
        
    while True:
        try:
            query = input("Search university by name, ID, or country (or Enter to list all, 'q' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
            
        if query.lower() in ("q", "quit", "exit"):
            print("Cancelled.")
            sys.exit(0)
            
        matches = filter_universities(universities, query)
        if not matches:
            print(f"No universities found matching '{query}'. Try another search term.\n")
            continue
            
        page_size = 25
        displayed = matches[:page_size]
        print(f"\nFound {len(matches)} match(es)" + (f" (showing first {page_size}):" if len(matches) > page_size else ":"))
        for i, u in enumerate(displayed, start=1):
            print(f"  [{i:2d}] {u['name']} ({u.get('country', 'N/A')}) — ID: {u['id']}")
            
        print("\nEnter number to select, 's' to search again, or 'q' to quit.")
        try:
            choice = input("Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
            
        if choice.lower() in ("q", "quit"):
            sys.exit(0)
        if choice.lower() == "s":
            continue
            
        if choice.isdigit() and 1 <= int(choice) <= len(displayed):
            selected = displayed[int(choice) - 1]
            return selected
        else:
            print("Invalid choice, please try again.\n")


def print_institution_details(u):
    print(f"\n✓ Active institution set to: {u['name']}")
    print(f"  • ID:             {u['id']}")
    print(f"  • Country:        {u.get('country', 'N/A')}")
    print(f"  • EZproxy Prefix: {u.get('ezproxy_prefix')}")
    print(f"  • Library Portal: {u.get('library_portal')}")
    print(f"  • Login Portal:   {u.get('login_portal')}")
    print(f"\nConfiguration saved to:")
    print(f"  • {ACTIVE_CONFIG_FILE.relative_to(ROOT)}")
    print(f"  • {ENV_FILE.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="Select university directly by ID (e.g. 'vanderbilt')")
    parser.add_argument("--search", "-s", help="Search query to filter universities")
    parser.add_argument("--list", "-l", action="store_true", help="List all supported universities")
    parser.add_argument("--current", "-c", action="store_true", help="Show currently configured active institution")
    parser.add_argument("--clear", action="store_true", help="Clear current active institution configuration")
    parser.add_argument("--non-interactive", action="store_true", help="Select best match without interactive prompt")
    args = parser.parse_args()

    universities = load_universities()

    if args.clear:
        if ACTIVE_CONFIG_FILE.exists():
            ACTIVE_CONFIG_FILE.unlink()
        print("Cleared active institution configuration.")
        return 0

    if args.current:
        current = get_active_institution()
        if current:
            print(f"Active institution: {current['name']} ({current['id']})")
            print(f"  EZproxy prefix: {current.get('ezproxy_prefix')}")
            print(f"  Library portal: {current.get('library_portal')}")
        else:
            print("No active institution configured. Run without arguments to select one.")
        return 0

    if args.list:
        print(f"Supported Universities ({len(universities)} total):")
        for u in universities:
            print(f"  • {u['id']:<15} {u['name']} ({u.get('country', 'N/A')})")
        return 0

    if args.id:
        target_id = args.id.lower().strip()
        matched = next((u for u in universities if u["id"].lower() == target_id), None)
        if not matched:
            parser.error(f"University with ID '{args.id}' not found. Use --list to see available IDs.")
        saved = save_active_institution(matched)
        print_institution_details(saved)
        return 0

    if args.search and args.non_interactive:
        matches = filter_universities(universities, args.search)
        if not matches:
            parser.error(f"No universities matched '{args.search}'.")
        saved = save_active_institution(matches[0])
        print_institution_details(saved)
        return 0

    # Interactive flow
    if not sys.stdin.isatty():
        # Non-interactive fallback: if Vanderbilt exists, default to it
        default_u = next((u for u in universities if u["id"] == "vanderbilt"), universities[0])
        saved = save_active_institution(default_u)
        print_institution_details(saved)
        return 0

    selected = interactive_selection(universities)
    saved = save_active_institution(selected)
    print_institution_details(saved)
    print("\nYou can now run 'python3 auth_setup.py' to authenticate with your university library portal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
