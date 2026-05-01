# Server Manager

数据中心 BMC 硬件监控工具，支持 **Redfish** 和 **IPMI** 双协议。  
通过 Web 界面直观查看所有服务器的温度、风扇、电源、处理器等硬件状态。

## 快速开始

### 安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/asdfec456-dot/servermanager/main/get.sh)
```

### 升级

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/asdfec456-dot/servermanager/main/get.sh) upgrade
```

### 卸载

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/asdfec456-dot/servermanager/main/get.sh) uninstall
```

---

安装完成后访问 `http://<服务器IP>:8080`，在右上角「设置」中填写 BMC IP 范围和登录凭据即可开始监控。

**支持格式：**

| 输入 | 说明 |
|---|---|
| `192.168.1.100` | 单个 IP |
| `192.168.1.100-200` | 末段范围 |
| `192.168.1.0/24` | CIDR 子网段 |

**支持厂商：** Dell iDRAC · HPE iLO · Supermicro · 华为 iBMC · 其他 Redfish/IPMI 设备

---

© 2026 [CATNETWORK](https://www.catnetwork.co.jp)
