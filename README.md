# Server Manager

数据中心 BMC 硬件监控工具，支持 **Redfish** 和 **IPMI** 双协议。  
通过 Web 界面直观查看所有服务器的温度、风扇、电源、处理器等硬件状态，并支持浏览器内 KVM 远程控制台。

---

## 快速安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/asdfec456-dot/servermanager/main/get.sh)
```

安装过程中会：

1. **检测系统语言环境** — 自动询问是否使用对应语言（中文 / 日文 / 英文）
2. **自动安装缺失组件** — `git` / `python3` / `ipmitool` 等
3. **Web 代理配置**（可选）— 输入域名、端口、公网 IP，配置前会显示完整操作清单并要求确认
4. **开机自启服务**（可选）— 注册为 systemd 服务

安装完成后，浏览器打开提示的地址，**首次访问**会要求设置集群名称和管理员账户。

---

## 升级 / 卸载

```bash
# 升级到最新版本
bash get.sh upgrade

# 卸载并清理
bash get.sh uninstall

# 单独重新配置 Web 代理
bash get.sh proxy
```

卸载时会询问是否移除 nginx / apache2 代理配置及自动安装的系统组件。

---

## 非交互模式（CI / 脚本）

```bash
# 通过环境变量传入参数，--yes 全自动
DOMAIN=sm.example.com PROXY_PORT=443 PUBLIC_IP=1.2.3.4 \
  bash get.sh install --yes

# 指定界面语言（zh / en / ja）
SM_LANG=en bash get.sh install --yes
```

---

## Web 代理配置

安装时脚本自动检测已有 Web 服务器并优先复用，配置前会显示所有将执行的操作并要求手动确认。

| 情况 | 行为 |
|---|---|
| 已安装 **nginx** | 写入 `/etc/nginx/sites-available/servermanager` 并 `reload` |
| 已安装 **apache2** | 写入 `/etc/apache2/sites-available/servermanager.conf` 并启用 |
| **两者都没有** | 询问安装哪个，或跳过 |
| **端口冲突** | 显示占用进程，提示输入新端口 |
| **域名冲突** | 显示冲突站点，提示输入新域名 |

所有被修改的配置文件均会在操作前自动备份（带时间戳）。

---

## 功能概览

### 硬件监控

- 温度 / 风扇 / 电源模块 / 内存 / CPU 状态
- 功耗实时统计
- GPU 检测（RTX / Tesla / Instinct 等 PCIe 设备）
- 多视图：卡片 / 列表 / 表格

### KVM 远程控制台

点击机器卡片上的 **▶ KVM** 按钮，在浏览器内直接打开远程控制台：

- **标准 VNC 机器**（AMI MegaRAC 等）— noVNC HTML5 查看器，支持缩放 / 全屏
- **ATEN IPMI 机器**（Supermicro 等）— 代理 BMC 内置 HTML5 iKVM，自动登录

> KVM 访问权限由**系统管理员**控制，可对每个账户单独开关。

### 告警管理

自定义告警规则（温度阈值 / 电源故障 / 机器离线等），支持 Telegram / Email / Webhook 推送。

---

## 用户角色

| 角色 | 创建方式 | 权限 |
|---|---|---|
| **系统管理员** | 系统初始化时自动创建，不可删除 | 全部权限 + 控制其他用户的 KVM 开关 |
| **管理员** | 由系统管理员创建 | 完整管理权限（不含 KVM 权限控制） |
| **游客** | 由管理员 / 系统管理员创建 | 仅可查看被授权的子集群 / 机器，KVM 默认关闭 |

### 子集群

将机器分组管理（同一台机器可属于多个子集群）：

- 管理员可创建子集群并控制游客的访问范围
- 游客登录后只看到被授权的内容

---

## 支持的 IP 格式

| 输入 | 说明 |
|---|---|
| `192.168.1.100` | 单个 IP |
| `192.168.1.100-200` | 末段范围 |
| `192.168.1.100-192.168.1.200` | 完整范围 |
| `192.168.1.0/24` | CIDR 子网段 |
| 多种格式混合，换行或逗号分隔 | ✓ |

## 支持的 BMC 厂商

Dell iDRAC · HPE iLO · Supermicro · 华为 iBMC · 其他 Redfish / IPMI 设备

---

## 端口说明

| 端口 | 说明 |
|---|---|
| **8080**（默认）| 应用内部端口，可通过 `PORT=xxxx` 环境变量修改 |
| **80 / 443**（代理）| nginx / apache2 对外端口，安装时自定义 |

---

## 多语言

安装时根据系统 `$LANG` 自动提示，Web 界面支持 🇨🇳 中文 · 🇺🇸 English · 🇯🇵 日本語，用户可随时在界面内切换。

---

© 2026 [CATNETWORK](https://www.catnetwork.co.jp)
