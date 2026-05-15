#!/bin/bash
set -e
exec > /var/log/tmp_switch.log 2>&1
echo "=== /tmp → /var/tmp switch started at $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="

# Step 1: Kill tmux server
echo "Step 1: Killing tmux server..."
tmux kill-server 2>/dev/null || true
sleep 2

# Step 2: Atomic rename /tmp → /tmp.old
echo "Step 2: mv /tmp /tmp.old"
if [ -d /tmp.old ]; then
    echo "ERROR: /tmp.old already exists, aborting"
    exit 1
fi
mv /tmp /tmp.old

# Step 3: Create symlink
echo "Step 3: ln -s /var/tmp /tmp"
ln -s /var/tmp /tmp

# Step 4: Copy everything back
echo "Step 4: cp -a /tmp.old/. /tmp/"
cp -a /tmp.old/. /tmp/

echo "=== /tmp → /var/tmp switch completed at $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
echo "VERIFY: ls -ld /tmp = $(ls -ld /tmp)"
echo "VERIFY: newuidmap: $(ls -la /tmp/newuidmap 2>&1)"
echo "VERIFY: newgidmap: $(ls -la /tmp/newgidmap 2>&1)"
echo "VERIFY: df /tmp: $(df /tmp | tail -1)"
echo ""
echo "POST-VERIFY: /tmp should show as symlink → /var/tmp"
echo "POST-VERIFY: /tmp/newuidmap and /tmp/newgidmap should exist"
echo "POST-VERIFY: df /tmp should show lv_tmp (10G, XFS, /dev/mapper/ocivolume-lv_tmp)"
echo ""
echo "CLEANUP: rm -rf /tmp.old (after confirming everything works)"
