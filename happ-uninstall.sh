#!/bin/bash
# Uninstall Happ VPN completely.
# Run from a terminal where you can authenticate with sudo.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
  echo "Don't run as root directly — run as your user, sudo will prompt."
  exit 1
fi

echo "This will REMOVE Happ VPN entirely. Continue? [y/N]"
read -r ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "Aborted."; exit 0; }

echo
echo "[1/6] Stop + disable Happ systemd units ..."
for unit in happ-singbox.service happ-xray.service happd.service happ-vpn.target; do
  sudo systemctl disable --now "$unit" 2>/dev/null || true
done

echo
echo "[2/6] Remove Happ systemd unit files ..."
for f in /etc/systemd/system/happ-singbox.service \
         /etc/systemd/system/happ-xray.service \
         /etc/systemd/system/happd.service \
         /etc/systemd/system/happd.service.d \
         /etc/systemd/system/happ-vpn.target; do
  sudo rm -rf "$f" && echo "  removed $f"
done
sudo systemctl daemon-reload

echo
echo "[3/6] Remove /opt/happ installation + symlink ..."
sudo rm -f /usr/sbin/happ
sudo rm -rf /opt/happ
echo "  removed /opt/happ + /usr/sbin/happ symlink"

echo
echo "[4/6] Remove /etc/happ-vpn config dir ..."
sudo rm -rf /etc/happ-vpn
echo "  removed /etc/happ-vpn"

echo
echo "[5/6] Remove user-level Happ artifacts ..."
rm -f ~/.local/share/applications/happ.desktop
rm -f ~/.local/lib/libhapp-exec-delay.so
rm -f ~/.local/lib/libhapp-execdelay.so
echo "  removed ~/.local/share/applications/happ.desktop"
echo "  removed ~/.local/lib/libhapp-exec-delay.so libhapp-execdelay.so"

echo
echo "[6/6] Verify nothing is left ..."
echo "  processes: $(pgrep -af happ | grep -v grep | grep -v uninstall || echo none)"
echo "  units:     $(systemctl list-unit-files 2>/dev/null | grep -i happ | grep -v uninstall || echo none)"
echo "  binary:    $(ls /usr/sbin/happ 2>&1 || echo gone)"
echo "  /opt/happ: $(ls -d /opt/happ 2>&1 || echo gone)"

echo
echo "Done. Any local Happ repo files you wish to keep should be moved"
echo "into an archive directory of your choice before running this script."
echo "(The original author archived them to happ-archived/ in their dotfiles repo.)"
