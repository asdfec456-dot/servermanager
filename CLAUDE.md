# Server Manager — Claude 工作规则

## UI 变更后必须用 Chrome 验证

**每次修改前端代码（index.html / main.py 返回 HTML 的接口）后，必须调用 Mac 上的 Chrome 进行 UI 检查。**

步骤：
```
1. 部署到服务器（git push → server git pull → systemctl restart）
2. 用 gstack browse 工具打开 Chrome，登录 https://sm.catnetwork.co.jp
   用户名：asdfec123  密码：123456
3. 截图验证改动效果，确认无视觉异常后才算完成
```

不可仅凭语法检查（`node --check` / `python3 -c "import ast"`）就宣告完成。

---

## 版本号

版本号维护在 `main.py` 的 `APP_VERSION` 变量，**每次 commit 必须更新版本号**。

- 大功能：`1.X.0`
- Bug 修复 / 小改动：`1.X.Y`（末位递增）

---

## 双仓库维护

| 仓库 | 用途 |
|------|------|
| `origin`（公开）| 不含系统安装功能 |
| `private`（私密）| 全功能，服务器运行的是这个版本 |

修改后两个仓库都要同步：
```bash
# 改完 origin/main 后同步到 private
git checkout private-main
git checkout origin/main -- main.py static/index.html  # 或具体改动文件
git commit -m "同步: <描述>"
git push private private-main:main
git checkout main
```

服务器从私密仓库拉取，用户名 `ipfs10`，密码 `5121024`，IP `210.164.16.126`。

---

## 设置菜单顺序规则

系统配置类（BMC 配置、HTTPS 配置）放最前，中间是管理功能，显示偏好放最后：

```
BMC 配置
HTTPS 配置
────────
子集群管理
用户管理
警报管理
────────
卡片显示
```
