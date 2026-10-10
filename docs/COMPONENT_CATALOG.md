# 设计器完整组件目录

## 目的

`shizai_rpa_components` 返回当前应用已持久化的请求专属计划、组件契约和下一步。它不选择站点流程。

`shizai_rpa_component_catalog` 是统一组件发现接口。它把两类能力分开返回：

- `scope=builtin`：当前设计器安装包的 `rpa-engine/componentDicts/componentsNew.json`，再合并设计器前端维护的两个专用入口。它代表安装设计器即有的基础组件。
- `scope=business`：当前登录账号的 `bizComponentInfo.json` 业务组件索引，以及本机 `child-module-packages/biz` 缓存和指定项目的 `projects/dependency_modules` 导入状态。它代表 SaaS 扩展；私有化部署使用前需要导入相应组件包。
- `scope=all`（默认）：基础组件在前、业务扩展在后，方便模型先做通用能力规划，再判断是否需要扩展包。

每次返回都包含 `planning_guidance.numeric_target` 参考。模型如果在分析结果中发现
`evidence.collection.max_rows`，应把它当作最终输出验收条件，再从完整实时目录中选择
合适的组件组合。组件内置分页、显式下一页、循环、JavaScript、下载等方案都可以使用；
`collect_items` 和分页控件只在所选组件契约支持且确实需要时配置。

接口不会自动导入业务包，也不会把组件目录直接拼进流程。组件目录用于意图分析和契约发现；真正写入流程前仍需按 native 项目格式校验。

组件目录来自当前本机设计器，因此不同版本的组件数量、版本和参数可能不同。生成流程时应以接口实时结果为准，不根据显示标题猜组件类型或输出变量。

## 查询接口

MCP：

```json
{
  "scope": "all",
  "query": "数据采集",
  "category": "",
  "component_type": "",
  "package": "WebBrowser",
  "capability": "navigate,input,submit,extract,limit,paginate,output",
  "capability_mode": "any",
  "page": 1,
  "limit": 20,
  "detail": "contract"
}
```

CLI：

```powershell
shizai-agent --json workflow rpa-component-catalog --query "数据采集" --limit 20 --detail contract
shizai-agent --json workflow rpa-component-catalog --category get_web_data --limit 20 --detail summary
shizai-agent --json workflow rpa-component-catalog --component-type WebBrowser_DataCrawl --detail full --limit 1
shizai-agent --json workflow rpa-component-catalog --scope builtin --top-level webBrowser --detail summary
shizai-agent --json workflow rpa-component-catalog --scope business --business-category 京东 --detail summary
shizai-agent --json workflow rpa-component-catalog --scope business --business-package 京准通-互动广告 --project <PROJECT_REF> --detail contract
```

参数说明：

| 参数 | 作用 |
| --- | --- |
| `query` | 在 `type`、标题、分类、描述和标签中搜索；多个词要求同时命中 |
| `scope` | `builtin`、`business` 或 `all`；默认 `all`，基础组件排在业务扩展前 |
| `category` | 精确筛选分类；新版设计器允许一个组件属于多个分类 |
| `component_type` | 精确筛选 native 类型，例如 `WebBrowser_DataCrawl` |
| `package` | 基础组件按包前缀筛选，例如 `WebBrowser`、`Excel`、`OCR`；业务组件按业务包名筛选 |
| `capability` | 以逗号分隔的能力，例如 `extract,limit,paginate,output` |
| `capability_mode` | `all` 要求组件覆盖全部能力；`any` 用于一次返回多角色候选 |
| `top_level` | 按设计器可见一级菜单 id 或中文名称筛选，例如 `webBrowser` 或 `网页浏览器` |
| `business_category` | 按业务大类筛选，例如 `京东`、`飞书` |
| `business_package` | 按业务组件包名称精确筛选，例如 `京准通-互动广告` |
| `project_ref` | 指定客户端项目后，返回业务包是否已导入该项目 |
| `page` / `limit` | 分页返回；`limit` 最大 200，避免上下文过大 |
| `detail` | `summary` 只返回摘要；`contract` 增加输入、选项、输出和代码；`full` 额外返回完整 definition |
| `engine_root` | 可选本地 `rpa-engine` 路径，用于测试不同设计器版本 |
| `business_info_path` / `business_cache_root` | 可选业务索引和缓存路径，便于私有化部署或测试 |

## 返回契约

每个组件至少包含：

- `scope`：`builtin` 或 `business`。
- `type`：写入 native `.task` 时使用的真实组件类型。
- `label`、`description`、`category`、`version`：给模型做能力匹配。
- `inputs`：`definition.args0` 的参数契约，包括 `name`、类型、默认值、是否导出、可见控件和校验规则。
- `options`：组件通用或高级选项，对应 `definition.options0`。
- `outputs`：`codeDesc.assignTo` 拆出的输出变量，以及 `isExport` 参数。
- `code`：设计器运行时调用的函数和参数顺序。
- `component_modes`、`package`、`tags`：运行模式与搜索辅助信息。
- `top_levels`：基础组件对应的设计器一级菜单。
- `availability`、`requires_import`：业务组件的包缓存/项目导入状态；基础组件固定为 `installed` 和 `false`。
- `capabilities`、`operation_types`：组件能覆盖的任务能力和分析操作类型。
- `planning_patterns`：常见目标对应的语义参数组合；例如按 N 条采集并翻页。
- `deferred_bindings`、`conditional_requirements`：需要页面证据后再绑定的参数及条件显示规则。

顶层统计字段：

- `counts.builtin.engine_records`：引擎字典记录数。
- `counts.builtin.designer_static_extra`：前端静态且不在引擎字典中的基础入口数。
- `counts.business.advertised`：业务索引中各包的发布组件总数。
- `counts.business.visible`：当前缓存中实际展开并带有叶子详情的记录数。
- `business_categories` / `business_packages`：业务分类、包名、发布总数、缓存状态和项目导入状态。

`outputs.primary` 只表示组件主要输出的建议名称。实际流程中的变量绑定仍需结合组件默认配置和当前草稿验证；不能只根据标题或 `primary` 直接创建变量。

## 推荐的模型调用策略

1. 先调用 `shizai_auth_status`，确认设计器已启动并登录。
2. 只调用一次 `shizai_rpa_analyze_request`，原样保留 query 和 analysis_id。
3. 使用分析返回的 `recommended_catalog_query` 做一次 `capability_mode=any + detail=contract` 查询。
4. 参考 `component_requirements` 选择能覆盖目标的组件组合；如果有已安装、可物化且能力匹配的原生组件，先评估它，结构化网页采集优先评估 `WebBrowser_DataCollectionV5`。N 条多页采集可以选择一个内置分页组件，也可以拆成采集、翻页、循环和输出等多个组件。分析结果若声明 `collection.shape=hierarchical`，按 `parent_collection`、`child_collection`、`record_alignment` 分阶段设计，两个采集节点使用不同输出目标并保留父子对齐键；MCP native-first 预检会在阶段不完整时阻止写入。
5. 把候选的 `planning_patterns` 当作语义 config 示例；不要复制 `defaultValue` 中的 native 引号编码，也不要把示例当成固定步骤。
6. 业务组件先检查 `availability`、`package_cache_available`、`project_imported` 和 `requires_import`；`catalog_only` 只能作为能力参考，不能直接写入项目。
7. 只有选定组件后才读取 `detail=full`，避免把完整 UI 配置大量放入上下文。
8. 客户端模型根据目标把操作映射到一个或多个已确认组件，按需要声明依赖、输入输出变量、元素角色和配置，先提交给 `shizai_rpa_validate_plan`。
9. 预检通过后用 `open_browser=false` 调用 `shizai_rpa_start_design`，再按返回的下一动作进行浏览器验证。

原生组件优先是默认建议，不是硬性选型规则。若模型改用 JS、下载、循环或其它已安装组合，
应记录原生组件不适用的原因并验证最终输出；目录不会因为未选 `WebBrowser_DataCollectionV5`
而拒绝合法计划。

## 版本与故障

如果返回 `ready=false`，说明当前 MCP 进程没有找到所选范围的本地目录。基础组件需要启动或安装实在设计器；业务索引需要设计器登录后加载。也可以设置 `SHIZAI_RPA_ENGINE_ROOT`、`SHIZAI_BIZ_COMPONENT_INFO`、`SHIZAI_BIZ_COMPONENT_CACHE`，或传对应路径参数。

如果组件目录中的类型存在，但当前 native 项目没有可复制的任务模板，先在设计器中创建一次该组件并保存，再进行 native 物化。目录可以提供参数契约，但不会凭空生成设计器专属的 task 节点结构。

## 与白皮书规则的对应

白皮书中的组件参数、输出变量、元素库和运行版本共同构成流程契约。本接口把安装版本的组件定义暴露给模型；流程校验和运行诊断继续负责检查元素绑定、包 ID、`.task` 索引、RuntimeHost 和输出文件。两个阶段必须同时通过，不能只因为组件查到了就认为流程完成。

## 7.3.7 本机核对结果

本机 `Z-Factory 7.3.7.0` 的基础目录核对结果：

- `componentsNew.json`：996 条唯一引擎组件定义。
- 设计器前端静态基础命令索引：14 项，其中 2 项不在引擎 JSON 中。
- MCP `scope=builtin`：998 个可发现基础类型，`designer_static_extra=2`。
- 设计器一级菜单按组件分类映射返回；一个组件属于多个内部分类时，会返回多个一级菜单。

本机业务索引核对结果：

- `bizComponentInfo.json`：25 个业务大类、244 个业务包，发布口径组件总数 1844。
- 当前缓存实际展开的叶子详情数为 1624；这不是发布总数，缺少叶子详情的包仍会以包级目录条目返回。
- “京东”发布口径为 221；当前索引展开 90 个叶子详情，其中京准通包有缓存 `.task` 模板，京东商智品牌版/商家版当前只有目录条目，需要导入对应业务包后再物化。

这些数量是运行时诊断结果，不写死在代码中；用户切换设计器版本、登录账号或业务包版本后，以接口的 `counts` 和 `business_categories` 为准。
