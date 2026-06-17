#!/bin/bash
# Run this once to push the cycling routes project to GitHub.
# Before running: create a private repo at https://github.com/new called "cycling-routes"

set -e
cd "$(dirname "$0")"

git init
git config user.email "daveybitter@gmail.com"
git config user.name "Dave Bitter"
git checkout -b main
git add generate_routes.py requirements.txt .gitignore .github/
git commit -m "Weekly cycling route generator"
git remote add origin https://github.com/davebitter/cycling-routes-generator.git
git push -u origin main

echo ""
echo "✓ Done! Now add secrets at:"
echo "  https://github.com/davebitter/cycling-routes-generator/settings/secrets/actions"
