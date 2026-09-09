发布 `0.7.26` Windows x64 包。

本版本包含原生组件优先的 advisory 规则：模型优先评估已安装且可物化的原生组件，
但 JS、下载、循环和其它合法方案仍可使用，并需要说明原因和验证输出。

验证：完整测试 `374 passed`；MCP initialize 成功，tools/list 返回 56 个工具；
最终资产仅包含 Windows x64 ZIP 和 SHA-256 校验文件。
