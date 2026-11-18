#!/usr/bin/env python3
"""Interactive Onboarding & Setup Wizard for Literature Research Skills.

Configures:
1. Reference Manager (Zotero, Paperpile, Mendeley, or EndNote)
2. Target Zotero Collection (default: "New")
3. University Library Selection & Institutional SSO Session Capture
4. Preferred Paper Download Directory
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import auth_setup
import select_university

ENV_FILE = ROOT / ".env"
CONFIG_DIR = ROOT / "config"
UNIVERSITIES_FILE = CONFIG_DIR / "universities.json"
ACTIVE_CONFIG_FILE = CONFIG_DIR / "active_institution.json"
DEFAULT_DOWNLOAD_DIR = "downloads"


def load_env_dict(env_path=None):
    """Load current key-value pairs from .env into a dictionary."""
    path = Path(env_path or ENV_FILE)
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                if k:
                    env[k] = v
    return env


def save_env_vars(updates, env_path=None):
    """Save or update environment variables in .env preserving formatting."""
    path = Path(env_path or ENV_FILE)
    lines = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    new_lines = []
    handled_keys = set()

    for line in lines:
        stripped = line.strip()
        matched_key = None
        for k in updates:
            if re.match(rf"^{k}\s*=", stripped):
                matched_key = k
                break

        if matched_key:
            val = updates[matched_key]
            # Quote if contains spaces
            if " " in str(val) and not (str(val).startswith('"') and str(val).endswith('"')):
                val = f'"{val}"'
            new_lines.append(f"{matched_key}={val}")
            handled_keys.add(matched_key)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in handled_keys:
            val = v
            if " " in str(val) and not (str(val).startswith('"') and str(val).endswith('"')):
                val = f'"{val}"'
            new_lines.append(f"{k}={val}")

    path.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")


def mask_secret(secret_str):
    """Mask sensitive API keys for display."""
    if not secret_str:
        return ""
    if len(secret_str) <= 8:
        return "*" * len(secret_str)
    return secret_str[:4] + "*" * (len(secret_str) - 8) + secret_str[-4:]


def prompt_reference_manager(current_env=None):
    """Prompt user to select reference manager and configure Zotero credentials."""
    env = current_env or load_env_dict()

    print("\n" + "=" * 68)
    print(" 📚 Step 1: Reference Manager Selection")
    print("=" * 68)
    print("Choose your primary citation & reference management tool:")
    print("  [1] Zotero     (Fully supported: automated cloud sync, PDF & Markdown notes)")
    print("  [2] Paperpile  (Google Workspace / Docs integration)")
    print("  [3] Mendeley   (Elsevier / Desktop & Web library)")
    print("  [4] EndNote    (Clarivate desktop & online library)")

    current_mgr = env.get("REFERENCE_MANAGER", "Zotero")
    default_opt = "1"
    if current_mgr.lower() == "paperpile":
        default_opt = "2"
    elif current_mgr.lower() == "mendeley":
        default_opt = "3"
    elif current_mgr.lower() == "endnote":
        default_opt = "4"

    try:
        raw_choice = input(f"\nSelect reference manager [1-4] (default: {default_opt}): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nWizard aborted.")
        sys.exit(0)

    choice = raw_choice or default_opt
    updates = {}

    if choice == "1":
        updates["REFERENCE_MANAGER"] = "Zotero"
        print("\n--- Zotero Cloud Integration Configuration ---")
        print("To enable automatic cloud sync, provide your Zotero User ID and API key.")
        print("(You can find or create these at: https://www.zotero.org/settings/keys)")

        # 1. Zotero User ID
        curr_user_id = env.get("ZOTERO_USER_ID", "")
        prompt_uid = f"Zotero User ID (numeric)" + (f" [{curr_user_id}]" if curr_user_id else "") + ": "
        uid_input = input(prompt_uid).strip()
        user_id = uid_input or curr_user_id
        if user_id:
            updates["ZOTERO_USER_ID"] = user_id

        # 2. Zotero API Key
        curr_key = env.get("ZOTERO_API_KEY", "")
        masked = mask_secret(curr_key)
        prompt_key = f"Zotero API Key" + (f" [{masked}]" if curr_key else "") + ": "
        key_input = input(prompt_key).strip()
        api_key = key_input or curr_key
        if api_key:
            updates["ZOTERO_API_KEY"] = api_key

        # 3. Target Zotero Collection
        curr_col = env.get("ZOTERO_COLLECTION_NAME", "New")
        col_input = input(f"Target Zotero Collection Name [default: {curr_col}]: ").strip()
        col_name = col_input or curr_col or "New"
        updates["ZOTERO_COLLECTION_NAME"] = col_name

        print(f"✓ Configured Zotero integration (Collection: '{col_name}').")

    elif choice == "2":
        updates["REFERENCE_MANAGER"] = "Paperpile"
        print("\nℹ️  Notice: Paperpile integration is currently in active development.")
        print("   When released, papers and structured literature notes will synchronize")
        print("   directly with your Paperpile library via Google Drive.")
        print("   In the meantime, verified offline intake packages (RIS citation, paper PDF,")
        print("   and structured notes) will be prepared for easy import.")

    elif choice == "3":
        updates["REFERENCE_MANAGER"] = "Mendeley"
        print("\nℹ️  Notice: Mendeley integration is currently in active development.")
        print("   Verified offline intake packages with standard RIS records and PDFs")
        print("   will be prepared for drag-and-drop intake into your Mendeley library.")

    elif choice == "4":
        updates["REFERENCE_MANAGER"] = "EndNote"
        print("\nℹ️  Notice: EndNote integration is currently in active development.")
        print("   High-fidelity RIS citation files and structured intake packages")
        print("   will be generated for seamless EndNote library imports.")

    else:
        print("Unrecognized option; defaulting to Zotero.")
        updates["REFERENCE_MANAGER"] = "Zotero"
        updates["ZOTERO_COLLECTION_NAME"] = env.get("ZOTERO_COLLECTION_NAME", "New")

    save_env_vars(updates)
    return updates


def prompt_university_and_auth(current_env=None, skip_auth=False):
    """Prompt user to select their university and optionally capture SSO session."""
    print("\n" + "=" * 68)
    print(" 🏛️  Step 2: University Library & Institutional SSO Setup")
    print("=" * 68)

    universities = select_university.load_universities()
    active_inst = select_university.get_active_institution()

    change_uni = True
    if active_inst:
        print(f"Current active institution: {active_inst.get('name')} [{active_inst.get('id')}]")
        print(f"EZproxy prefix: {active_inst.get('ezproxy_prefix')}")
        try:
            ch = input("Keep this institution? [Y/n]: ").strip().lower()
            if ch in ("", "y", "yes"):
                change_uni = False
        except (EOFError, KeyboardInterrupt):
            print("\nWizard aborted.")
            sys.exit(0)

    selected_uni = active_inst
    if change_uni:
        selected_uni = select_university.interactive_selection(universities)
        if selected_uni:
            select_university.save_active_institution(selected_uni)
            select_university.print_institution_details(selected_uni)

    # Check institutional session state
    has_session = auth_setup.get_existing_state_path() is not None

    if skip_auth:
        print("Skipping institutional browser authentication (--skip-auth).")
        return selected_uni

    print("\n--- Institutional SSO Session Capture ---")
    if has_session:
        print("✓ An existing institutional SSO session state was found in state/storage_state.json.")
        try:
            reauth = input("Would you like to re-authenticate with your university SSO now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return selected_uni

        if reauth not in ("y", "yes"):
            print("Keeping existing SSO session.")
            return selected_uni

    print(f"\nLaunching browser to authenticate with {selected_uni.get('name')}...")
    print("👉 Please log in with your university credentials and complete MFA in the browser window.")
    try:
        auth_setup.login(institution=selected_uni, headless=False)
    except Exception as e:
        print(f"⚠️  Note on SSO login: {e}")
        print("You can run 'python3 scripts/auth_setup.py' anytime to capture or refresh your session.")

    return selected_uni


def prompt_download_directory(current_env=None):
    """Prompt for preferred paper download directory and save to .env."""
    env = current_env or load_env_dict()

    print("\n" + "=" * 68)
    print(" 📁 Step 3: Paper Download Directory")
    print("=" * 68)

    current_dir = env.get("PAPER_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)
    print("Where should downloaded scientific papers (PDFs) be saved by default?")
    print(f"Current setting: {current_dir}")

    try:
        user_input = input(f"Preferred download directory [default: {current_dir}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nWizard aborted.")
        sys.exit(0)

    chosen_dir = user_input or current_dir or DEFAULT_DOWNLOAD_DIR

    # Expand ~ if provided
    resolved_path = Path(chosen_dir).expanduser()
    if not resolved_path.is_absolute():
        resolved_path = ROOT / chosen_dir

    try:
        resolved_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"  Note: could not create directory {resolved_path} ({e})")

    updates = {"PAPER_DOWNLOAD_DIR": chosen_dir}
    save_env_vars(updates)
    print(f"✓ Default download directory set to: {chosen_dir}")
    return chosen_dir


def print_summary():
    """Display final summary of configured settings."""
    env = load_env_dict()
    inst = select_university.get_active_institution()
    has_session = auth_setup.get_existing_state_path() is not None

    print("\n" + "=" * 68)
    print(" 🎉 Literature Research Skills Setup Complete!")
    print("=" * 68)
    ref_mgr = env.get("REFERENCE_MANAGER", "Zotero")
    print(f"  • Reference Manager:   {ref_mgr}")
    if ref_mgr == "Zotero":
        print(f"  • Target Collection:   {env.get('ZOTERO_COLLECTION_NAME', 'New')}")
        print(f"  • Zotero User ID:      {env.get('ZOTERO_USER_ID', 'Not set (offline packages only)')}")
        has_key = bool(env.get("ZOTERO_API_KEY"))
        print(f"  • Zotero API Key:      {'Configured' if has_key else 'Not set'}")
    if inst:
        print(f"  • Active Institution:  {inst.get('name')} [{inst.get('id')}]")
        print(f"  • EZproxy Gateway:     {inst.get('ezproxy_prefix')}")
    print(f"  • SSO Session State:   {'Authenticated (state/storage_state.json)' if has_session else 'Not authenticated (run python3 scripts/auth_setup.py)'}")
    print(f"  • Download Directory:  {env.get('PAPER_DOWNLOAD_DIR', 'downloads')}")
    print("=" * 68)
    print("\nReady to retrieve papers! Try running the end-to-end pipeline:")
    print('  python3 scripts/fetch_and_process.py --doi "10.1016/j.tics.2006.10.012"')
    print('  # or for Open Access papers (e.g. eLife, PLOS, Frontiers):')
    print('  python3 scripts/fetch_and_process.py --doi "10.1371/journal.pone.0210000"\n')


def run_wizard(skip_auth=False):
    """Run the complete interactive setup wizard."""
    print("=" * 68)
    print(" 🚀 Literature Research Skills — Interactive Setup Wizard")
    print("=" * 68)
    print("Welcome! This onboarding wizard will configure your reference manager,")
    print("target collection, university library EZproxy routing, and storage paths.")

    current_env = load_env_dict()
    prompt_reference_manager(current_env)
    prompt_university_and_auth(current_env, skip_auth=skip_auth)
    prompt_download_directory(current_env)
    print_summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-auth", action="store_true", help="Skip launching browser for institutional SSO login")
    parser.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode with default/env values")
    args = parser.parse_args()

    if args.non_interactive:
        current_env = load_env_dict()
        if not current_env.get("REFERENCE_MANAGER"):
            save_env_vars({"REFERENCE_MANAGER": "Zotero", "ZOTERO_COLLECTION_NAME": "New"})
        if not current_env.get("PAPER_DOWNLOAD_DIR"):
            save_env_vars({"PAPER_DOWNLOAD_DIR": DEFAULT_DOWNLOAD_DIR})
        print_summary()
        return 0

    run_wizard(skip_auth=args.skip_auth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
