#!/usr/bin/env bash
set -e

echo "=== 1. Creating Python virtual environment ==="
python3.12 -m venv .venv || python3 -m venv .venv

echo "=== 2. Activating virtual environment and installing dependencies ==="
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements/connectors.lock.txt -r requirements/helpers.txt

echo "=== 3. Initializing portable skills and configuration ==="
python3 scripts/manage.py install-skills --dest "$HOME/.agents/skills"
python3 scripts/manage.py config --format json

echo "=== 4. Running environment health doctor ==="
python3 scripts/manage.py doctor

echo ""
echo "=== 5. Launching Interactive Onboarding Wizard ==="
if [ -t 0 ]; then
    read -p "Would you like to run the interactive setup wizard now? [Y/n] " run_wiz
    if [[ "$run_wiz" =~ ^[Yy]?$ ]]; then
        python3 scripts/setup_wizard.py
    else
        echo "You can launch the setup wizard anytime with:"
        echo "  python3 scripts/setup_wizard.py"
    fi
else
    echo "To configure your reference manager, target collection, and university SSO, run:"
    echo "  python3 scripts/setup_wizard.py"
fi

echo ""
echo "=== Setup complete! Your lit search pipeline is ready. ==="
