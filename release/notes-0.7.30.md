# 发布 `0.7.30`

本版修复 MCP 增量追加原生组件时的计划同步问题：

- 数据库、JSON 等目录组件的逻辑输出会从实时契约自动生成，不再被写成空输出数组。
- 追加或插入组件后，模型计划会按当前 Native MAIN_COMPONENT 顺序重建线性依赖，清理已删除步骤留下的旧依赖。
- 因此 `Database_SQLExecute`、`JSON_Parse`、`Database_BatchInsertExecute` 和 `Database_CloseDBConnect` 可以继续追加到已有流程并通过静态校验。

抖音评论流程仍由一个 XPath JavaScript 组件完成嵌套评论展开；数据库建表、JSON 解析、批量插入和关闭连接全部使用实在 Agent 原生组件。
