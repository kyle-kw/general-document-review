# Worker 的 Linux 隔离配置

`seccomp-bwrap.json` 基于 Moby 官方默认规则：
https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json

许可证保存在 `MOBY-LICENSE`。末尾追加的规则允许 bubblewrap 建立非特权 user/mount/PID/network/IPC/UTS namespace；保留默认拒绝策略、clone3 的 ENOSYS 回退及其他默认限制。目标架构 Linux amd64。

Compose 中 worker 使用非 root 用户，移除所有容器 capabilities，启用 no-new-privileges、此 seccomp 配置、2 GiB 内存和 512 个进程的容器总上限。没有 privileged、SYS_ADMIN 或 seccomp=unconfined。沙箱建立完成后，可信入口再次加载 seccomp，禁止 Agent 创建/加入 namespace、挂载和访问其他进程内存。

宿主 Linux 内核必须启用非特权 user namespace，Docker 必须允许该配置中的 namespace 系统调用。带额外 AppArmor/SELinux 策略的宿主还需由管理员允许镜像内 `/usr/bin/bwrap` 创建非特权 user namespace；不要关闭整个安全模块。预检失败会拒绝启动 worker，不会降级。Windows 开发 API/前端不需要该能力；实际审查使用 Linux/WSL2 worker。

在目标环境检查：

```sh
docker compose run --rm --no-deps worker /app/backend/.venv/bin/python -m app.worker --preflight
```

预检会实际运行 Python，验证输入只读、环境变量清空、宿主路径不可见、网络不可达、符号链接拒绝和后台进程清理。每次部署到不同内核或安全策略下都应执行。
