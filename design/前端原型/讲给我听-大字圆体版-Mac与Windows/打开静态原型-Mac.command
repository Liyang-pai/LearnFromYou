#!/bin/sh
# Open the sibling HTML file without starting a service.
prototype_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
open "$prototype_dir/index.html"
