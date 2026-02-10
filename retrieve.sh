#!/bin/bash

directory="src/replicator"

# Files to exclude (by basename)
exclude_files=(
    "__init__.py"
    "migration.py"
)

for file in "$directory"/*; do
    [ -f "$file" ] || continue

    name="$(basename "$file")"
    skip=false

    for excluded in "${exclude_files[@]}"; do
        if [[ "$name" == "$excluded" ]]; then
            skip=true
            break
        fi
    done

    $skip && continue
    echo "\`\`\`python"
    cat "$file"
    echo "\`\`\`"
done
