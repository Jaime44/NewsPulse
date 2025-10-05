#!/bin/bash
# -----------------------------------------------------------------------------
# Script: create_venv.sh
# Description: 
#   This script automates the creation and setup of a Python virtual environment.
#   Features:
#     1. Creates a virtual environment with a specified name or defaults to 'myvenv'.
#     2. If the environment already exists, it asks whether to overwrite it.
#        - If overwritten, the old environment is deleted and a new one is created.
#        - If not overwritten, the user can choose to verify that all packages in 
#          'requirements.txt' are installed in the existing environment, or abort.
#     3. Upgrades pip to the latest version in the virtual environment.
#     4. Installs packages from 'requirements.txt':
#        - Skips any package that fails to install, reporting it without stopping the script.
#        - Keeps track of failed installations in 'failed_packages.txt'.
#     5. After setup, optionally prompts the user to activate the virtual environment.
#
# Usage: ./create_venv.sh [env_name]
#   env_name: Optional. Name of the virtual environment (default: 'myvenv')
#
# Example: 
#   ./create_venv.sh myenv
# -----------------------------------------------------------------------------


set -e  # Exit on error for critical parts (creating venv, pip upgrade)

# Use parameter or default to 'myvenv'
ENV_NAME="${1:-myvenv}"

# Check if the virtual environment already exists
if [ -d "$ENV_NAME" ]; then
    # Ask user if they want to overwrite the existing environment
    read -rp "Virtual environment '$ENV_NAME' already exists. Overwrite? (Y/N): " yn
    case $yn in
        [Yy]* )
            rm -rf "$ENV_NAME"
            echo "Old environment removed."
            echo "Creating virtual environment '$ENV_NAME'..."
            python3 -m venv "$ENV_NAME"
            echo "Virtual environment created successfully."
            ;;
        [Nn]* )
            # Ask if user wants to verify packages
            read -rp "Do you want to verify that all packages in requirements.txt are installed in '$ENV_NAME'? (Y/N): " verify
            case $verify in
                [Yy]* )
                    echo "Verifying packages..."
                    # Loop through each package in requirements.txt
                    while IFS= read -r pkg; do
                        pkg_name=$(echo "$pkg" | cut -d'=' -f1)  # extract package name without version
                        if ! "$ENV_NAME/bin/pip" show "$pkg_name" >/dev/null 2>&1; then
                            echo "WARNING: Package '$pkg_name' is missing in '$ENV_NAME'"
                        fi
                    done < requirements.txt
                    echo "Package verification completed."
                    exit 0  # <--- TERMINATE script after verification
                    ;;
                [Nn]* )
                    echo "Aborting."
                    exit 1
                    ;;
                * )
                    echo "Invalid input. Aborting."
                    exit 1
                    ;;
            esac
            ;;
        * )
            echo "Please answer Y or N."
            exit 1
            ;;
    esac
else
    echo "Creating virtual environment '$ENV_NAME'..."
    python3 -m venv "$ENV_NAME"
    echo "Virtual environment created successfully."
fi


# Check if requirements.txt exists
if [ ! -f "requirements.txt" ]; then
    echo "Error: 'requirements.txt' not found in the current directory."
    exit 1
fi

# Upgrade pip first
"$ENV_NAME/bin/pip" install --upgrade pip

# Install packages one by one, skipping errors
# Install packages one by one, trying a fallback if installation fails
echo "Installing dependencies from 'requirements.txt'..."
while IFS= read -r package || [ -n "$package" ]; do
    # Skip empty lines and comments
    [[ -z "$package" || "$package" == \#* ]] && continue

    echo "Installing $package..."
    if "$ENV_NAME/bin/pip" install "$package"; then
        echo "$package installed successfully."
    else
        echo "WARNING: Failed to install $package. Trying to install latest compatible version..."

        # Extract package name and major version if specified
        pkg_name=$(echo "$package" | cut -d'=' -f1)
        major_version=$(echo "$package" | grep -oP '(?<===)\d+' || true)

        if [ -n "$major_version" ]; then
            echo "Trying $pkg_name>=${major_version}.0.0,<$(($major_version + 1)).0.0"
            "$ENV_NAME/bin/pip" install "$pkg_name>=${major_version}.0.0,<$(($major_version + 1)).0.0" || \
                echo "WARNING: Still failed to install $pkg_name. Skipping."
        else
            echo "Trying latest version of $pkg_name"
            "$ENV_NAME/bin/pip" install "$pkg_name" || echo "WARNING: Still failed to install $pkg_name. Skipping."
        fi
    fi
done < requirements.txt


echo "Installation process completed."

# Ask user if they want to activate the environment
while true; do
    read -rp "Do you want to activate the virtual environment now? (Y/N): " yn
    case $yn in
        [Yy]* )
            echo "Activating '$ENV_NAME'..."
            # shellcheck disable=SC1090
            source "$ENV_NAME/bin/activate"
            echo "Virtual environment '$ENV_NAME' is now active."
            break
            ;;
        [Nn]* )
            echo "You chose not to activate the environment. To activate later, run:"
            echo "    source $ENV_NAME/bin/activate"
            break
            ;;
        * ) echo "Please answer Y (yes) or N (no).";;
    esac
done
