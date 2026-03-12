# Setup Instructions for Multiroom Demo on macOS

This project uses advanced Linux kernel features (`network namespaces`, `macvlan`, `ALSA loopback`) to create isolated AirPlay 2 zones. These features are **not available on macOS**.

To run this on a Mac, you must use a **Linux Virtual Machine (VM)**.

## Option 1: Using Multipass (Recommended for CLI)

Multipass is the quickest way to get an Ubuntu shell on macOS.

### 1. Install Multipass
Open your terminal and run:
```bash
brew install --cask multipass
```

### 2. Launch an Ubuntu Instance
Create a new instance with enough resources (2 CPUs, 2GB RAM):
```bash
multipass launch --name shiri-demo --cpus 2 --mem 2G --disk 10G 22.04
```

### 3. Mount the Code
Mount your current directory into the VM so you can access the scripts:
```bash
# Assuming you are in the directory containing 'multiroom-demo'
multipass mount . shiri-demo:/home/ubuntu/shiri
```

### 4. Shell into the VM
```bash
multipass shell shiri-demo
```

### 5. Install Dependencies (Inside VM)
Once inside the VM shell, navigate to the folder and run the installer:
```bash
cd ~/shiri/multiroom-demo
sudo ./install_deps_ubuntu.sh
```
*Note: This will update `apt`, install build tools, compile `nqptp` and `shairport-sync`, and install `owntone`.*

### 6. Run the Demo (Inside VM)
```bash
sudo ./dual_zone_demo.sh
```

---

## Option 2: Using UTM / VirtualBox (For Real Network Access)

**Important Limitation of Multipass**: By default, Multipass uses NAT networking. The `macvlan` feature used in this script requires a **Bridged Network** to be fully functional (so your iPhone can see the AirPlay devices).

If standard Multipass networking doesn't work (i.e., you can't see "Shiri Zone 1" on your phone), use **UTM** or **VirtualBox**:

1.  **Install Ubuntu 22.04 Server** normally in the VM.
2.  **Configure Network to "Bridged Adapter"** (this connects the VM directly to your Wi-Fi/Ethernet, getting its own IP).
3.  **Enable Promiscuous Mode** on the VM network adapter if using VirtualBox (check "Allow All").
4.  **Copy the code** to the VM (via `scp` or git clone).
5.  Run the installation scripts as above:
    ```bash
    sudo ./install_deps_ubuntu.sh
    sudo ./dual_zone_demo.sh
    ```

## Troubleshooting

-   **"Missing required commands"**: Run `sudo ./install_deps_ubuntu.sh` again to ensure everything installed.
-   **"modprobe: FATAL: Module snd-aloop not found"**: The kernel module for audio loopback is missing.
    -   Try: `sudo apt install linux-modules-extra-$(uname -r)`
-   **Can't see AirPlay devices**:
    -   Verify your VM is on the **same network** as your phone (Bridged Mode).
    -   Check if the firewall is blocking mDNS (Avahi).
