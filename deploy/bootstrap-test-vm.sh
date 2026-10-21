#!/bin/sh
# Dedicated OLCWave test VM. Run on pve.home.arpa; never clone a live VPN VM.
set -eu
VMID=100
IMAGE=/var/lib/vz/template/iso/debian-13-genericcloud-amd64-20261021-2638.qcow2
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/20261021-2638/debian-13-genericcloud-amd64-20261021-2638.qcow2
SHA512=16e0965312c56feb331049d45d2b1b5778f3f942a33d4300eedbb97527707f2bea87c0006f3e4cc7a25fadbff739c4e235a51fb9bcd1b66fd554f7f8e80ba45c
if qm status "$VMID" >/dev/null 2>&1; then
    echo 'VM100 already exists; refusing to overwrite it' >&2
    exit 1
fi
install -d /var/lib/vz/template/iso
curl --fail --location --output "$IMAGE" "$IMAGE_URL"
printf '%s  %s\n' "$SHA512" "$IMAGE" | sha512sum -c -
# Agent's public key is provided separately, never a private SSH key.
test -s /root/olcwave-test.pub
qm create "$VMID" --name olcwave-test --description 'Dedicated OLCWave/Ghostlane acceptance VM; no production users' --memory 4096 --balloon 2048 --cores 2 --cpu host --net0 virtio,bridge=vmbr0,tag=100 --scsihw virtio-scsi-pci --serial0 socket --vga serial0 --agent enabled=1 --onboot 0
qm importdisk "$VMID" "$IMAGE" local-lvm
qm set "$VMID" --scsi0 local-lvm:vm-100-disk-0 --ide2 local-lvm:cloudinit --boot order=scsi0 --ciuser root --sshkeys /root/olcwave-test.pub --ipconfig0 ip=10.69.1.100/24,gw=10.69.1.1 --nameserver 10.69.1.1 --searchdomain home.arpa
qm resize "$VMID" scsi0 32G
qm start "$VMID"
qm config "$VMID"
