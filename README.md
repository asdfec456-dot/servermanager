# Server Manager

数据中心 BMC 硬件监控工具，支持 **Redfish** 和 **IPMI** 双协议。  
通过 Web 界面直观查看所有服务器的温度、风扇、电源、处理器等硬件状态。

---

## 快速安装

```bash
curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash
```

安装过程中会依次引导：

1. **缺失组件自动安装** — `git` / `python3` / `ipmitool` 等
2. **Web 代理配置**（可选）— 输入域名、端口、公网 IP
3. **开机自启服务**（可选）— 注册为 systemd 服务

安装完成后，浏览器打开提示的地址，**首次访问**会要求设置集群名称和管理员账户。

---

## 升级

```bash
curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s upgrade
```

---

## 卸载

```bash
curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s uninstall
```

卸载时会询问是否：
- 移除 nginx / apache2 的代理配置
- 卸载安装时自动安装的系统组件

---

## Web 代理配置详解

安装时，脚本会询问是否配置域名访问：

```
[*] Web 代理配置（将域名/端口映射到应用）
  域名（留空则跳过代理配置）: sm.example.com
  外部访问端口 [80]: 80
  本机公网 IP（留空则自动检测）:
```

脚本会自动：

| 情况 | 行为 |
|---|---|
| 已安装 **nginx** | 写入 `/etc/nginx/sites-available/servermanager` 并 `reload` |
| 已安装 **apache2** | 写入 `/etc/apache2/sites-available/servermanager.conf` 并启用 |
| **两者都没有** | 自动安装 apache2，然后配置 |
| **端口冲突** | 显示占用进程，提示输入新端口 |
| **域名冲突** | 显示冲突站点，提示输入新域名 |

### 安装后单独配置或更新代理

```bash
curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s proxy
```

### 非交互模式（CI / 脚本）

通过环境变量传入代理参数，配合 `--yes` 全自动安装：

```bash
DOMAIN=sm.example.com PROXY_PORT=80 PUBLIC_IP=1.2.3.4 \
  curl -fsSL https://asdfec456-dot.github.io/servermanager/get.sh | bash -s install --yes
```

---

## 用户与权限

首次访问 Web 界面时，必须完成初始化设置：

- **集群名称** — 如 "CATNETWORK IDC Tokyo"
- **管理员用户名 + 密码**

之后管理员可在界面中：

- **添加管理员** — 拥有全部权限
- **添加查看者** — 仅可查看被授权的子集群

### 子集群

将机器分组为子集群（同一台机器可属于多个子集群）：

- 管理员：右上角「子集群」按钮
- 查看者：登录后只看到被授权的子集群，按组显示

---

## 支持格式

### BMC IP 地址格式

| 输入 | 说明 |
|---|---|
| `192.168.1.100` | 单个 IP |
| `192.168.1.100-200` | 末段范围 |
| `192.168.1.100-192.168.1.200` | 完整范围 |
| `192.168.1.0/24` | CIDR 子网段 |
| 多种格式混合，换行或逗号分隔 | ✓ |

### 支持的 BMC 厂商

Dell iDRAC · HPE iLO · Supermicro · 华为 iBMC · 其他 Redfish/IPMI 设备

---

## 端口说明

| 端口 | 说明 |
|---|---|
| **8080**（默认）| 应用内部端口，可通过 `PORT=xxxx` 环境变量修改 |
| **80**（代理） | nginx/apache2 对外暴露的端口，可在安装时自定义 |

---

© 2026 [CATNETWORK](https://www.catnetwork.co.jp)
