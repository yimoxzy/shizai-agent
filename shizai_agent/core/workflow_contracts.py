"""Small, model-visible authoring contracts; no business planning or site recipes."""

from typing import Any


def rpa_plan_schema(*, for_creation: bool = False) -> dict[str, Any]:
    strings = {"type": "array", "items": {"type": "string"}}
    logical_outputs = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "逻辑变量名非var_name/var0通常省略"
        ),
    }
    return {
        "type": "object",
        "description": "collection_shape=hierarchical 表示层级记录；config 传语义值。",
        "properties": {
            "analysis_id": {"type": "string"},
            "name": {"type": "string"},
            "target": {"type": "object"},
            "steps": {
                "type": "array", "minItems": 1,
                "description": "主流程按此顺序写入；depends_on 是依赖声明，不会生成分支或循环。当前接口没有嵌套循环体/条件分支写图结构。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "description": "稳定且唯一；后续编辑沿用此 id。"},
                        "component": {"type": "string", "minLength": 1},
                        "purpose": {"type": "string"},
                        "config": {"type": "object", "description": '变量引用用 {"variable":"变量名"}；字符串（包括 $name）是字面量。'},
                        "depends_on": strings, "inputs": strings, "outputs": logical_outputs,
                        "element_ids": strings, "operation_ids": strings,
                        "element_role": {"type": "string"},
                        "collection_stage": {
                            "type": "string",
                            "description": "原生采集阶段，例如 parent_collection 或 child_collection。",
                        },
                        "collection_role": {"type": "string"},
                        "parent_step_id": {"type": "string"},
                        "merge_key": {"type": "string"},
                        "native_alternative_reason": {"type": "string"},
                    },
                    "required": ["id", "component", "config"],
                    "additionalProperties": True,
                },
            },
            "output_contract": {
                "type": "object",
                "description": "输出验收约定；file 必须与实际写文件组件的目标一致，不会自动配置组件参数。",
                "properties": {
                    "file": {"type": "string", "description": "主要输出文件的完整路径；运行状态据此查验真实产物。"},
                    "columns": strings,
                    "max_rows": {"type": "integer", "minimum": 0, "description": "用户要求的最少有效数据行数；未要求时省略。"},
                },
                "additionalProperties": True,
            },
            "validation_mode": {"type": "string", "default": "flexible"},
        },
        "required": ["analysis_id", "name", "steps", "output_contract"] if for_creation else ["steps"],
        "additionalProperties": True,
    }


# Opt-in RPA authoring surface. Keep execution, capture recovery and stop tools;
# omit Agent graph, duplicate catalog surfaces and UI-only diagnostics.
RPA_AUTHORING_TOOLS = frozenset({
    "shizai_auth_status", "shizai_project_list", "shizai_extension_status",
    "shizai_browser_status", "shizai_browser_tabs", "shizai_browser_activate_tab", "shizai_browser_capture_viewport",
    "shizai_browser_inspect_points", "shizai_browser_infer_collection", "shizai_browser_action",
    "shizai_rpa_analyze_request", "shizai_rpa_component_catalog", "shizai_rpa_validate_plan",
    "shizai_rpa_start_design", "shizai_rpa_read_flow", "shizai_rpa_update_step", "shizai_rpa_add_step",
    "shizai_rpa_reload_editor", "shizai_rpa_run_component", "shizai_rpa_remove_step",
    "shizai_rpa_save_editor",
    "shizai_rpa_open_browser", "shizai_rpa_capture_windows", "shizai_rpa_scan_page",
    "shizai_rpa_capture_page_fields", "shizai_rpa_capture_elements",
    "shizai_project_import_elements",
    "shizai_extension_start_capture", "shizai_extension_stop_capture",
    "shizai_rpa_browser_session_start", "shizai_rpa_browser_session_stop",
    "shizai_rpa_validate_step", "shizai_rpa_validate_flow", "shizai_rpa_runtime_preflight",
    "shizai_rpa_sync_diagnose",
    "shizai_rpa_debug_capabilities", "shizai_rpa_debug_start",
    "shizai_rpa_debug_status", "shizai_rpa_debug_command", "shizai_rpa_debug_stop",
    "shizai_rpa_run", "shizai_rpa_status", "shizai_rpa_stop",
})

RPA_AUTHORING_INSTRUCTIONS = """你通过共享 CLI 核心设计原生 RPA，业务方案由你依据用户原句和实时组件目录决定。
新建：analyze_request 一次 → component_catalog 按能力查询 → validate_plan → start_design，open_browser=false。
编辑：已知 flow_ref 时先 read_flow(detail=edit)；未知时用带 query 的 project_list 查一次。选择稳定 step_id 后读取该步骤；update_step(dry_run=true) 预览差异，提交时带 expected_revision 和 allow_write=true；客户端已打开该流程时调用 reload_editor，再用 run_component 精确运行目标步骤并核对生成代码/缓存；冲突后重读，不重建项目。
清理历史节点用 remove_step：先 read_flow(detail=edit) 取 revision，dry_run=true 预览；被删步骤的输出变量若仍被下游引用会返回 step_has_dependents，应先改绑下游，确需强删才传 allow_dependents=true；删除后同样 reload_editor + run_component 复核。
计划步骤通常省略 outputs，由 MCP 从实时组件契约生成逻辑变量名；禁止把目录中的 outputs.assign_to（var_name/var0 等原生槽位）复制到计划 outputs。
本工具集可先保存完整流程骨架，但网页任务必须边设计边运行：先取得页面，再补齐当前步骤的元素绑定并单步验证，看到真实页面变化后继续下一步。结果页结构化采集优先一次调用 capture_page_fields 为 DataCollectionV5 绑定同一条记录的全部输出字段；分页控件用 scan_page 后调用 capture_elements(as_page_selector=true) 单独绑定。目录先 summary 筛选，选定 type 后 contract；不要反复拉取全目录。
结构化网页采集必须先评估原生采集，不能仅凭 extract+output 标签断言适用；优先分成页面准备、原生采集、数据处理/数据库写入。层级数据（例如父记录和子记录）必须拆成 parent_collection → child_collection → record_alignment 阶段；两个采集阶段使用不同输出变量、字段绑定和输出文件，并在后续步骤声明 parent_step_id/merge_key。MCP native-first 预检返回 stage_plan_required 时会拒绝 start_design 写入，先补齐这些阶段；平面数据才使用单个最终采集节点。比较候选契约与当前页面字段、XPath、父子关系和分页；适用时使用原生采集，不适用时在步骤 native_alternative_reason 中记录具体限制与验证证据。JS 可补足原生能力缺口，禁止因代码容易生成而绕过组件，禁止先用 JS 提取整表再放一个空壳采集节点。需要嵌套循环而接口未支持时报告写图缺口，不能把伪循环或长脚本称为原生组件方案。
网页元素优先走独立 MCP 浏览器扩展：browser_status → browser_tabs；目标 tab 或窗口不在前台时先调用 browser_activate_tab，再调用 browser_capture_viewport。直接观察返回图片，用少量视觉坐标调用 browser_inspect_points，不向模型枚举整棵 DOM。绑定输入/点击元素时同时传 project_ref、step_id、allow_write=true；重复列表先命中同一商品卡片中的至少两个字段，再用 browser_infer_collection 推断并绑定 DataCollectionV5。页面改变后重新截图，旧 screenshotId/pageRevision 不可复用。browser_action 只推进独立设计证据页，原生组件的 debug next 才是运行验证。扩展不可用或本地推断失败时才使用原 DeepCapture 路径。
先按实时契约选创建/获取网页对象的组件；跳转或操作已有对象的组件不能代替对象创建。对象参数用上游输出的变量引用，不能填 chrome、默认变量标签等字符串。保存骨架后立即 open_browser 打开计划目标页，再 capture_windows → scan_page；open_browser 只准备绑定页面，不能当作原生导航节点验证成功。
输入、点击、提交等改变页面状态的组件，绑定后用 debug_start/复用 → debug_command(next) → debug_status 验证当前步骤；查询结果页真正出现后才扫描并绑定采集字段。网页元素必须从当前 DeepCapture 页面捕获，scan_token 只用于同一快照；页面变化后重扫，仅明确替换旧绑定时 allow_rebind=true。官方拾取用 extension_start_capture，读取会话用 browser_session_start。
同一页面和同一步骤的自动扫描失败后最多提高一次深度。再次失败时，只有用户允许参考预设工作流，且来源元素是扩展标记的 deep_capture + verified，才可用 shizai_project_import_elements(source_project_ref, element_ids) 复用；必须保留来源并在当前页面单步验证。禁止复制未验证 selector、写入站点硬编码或继续空 selector 运行。
调试启动后若修改参数或绑定，旧进程不会热加载修改；返回 requires_restart 时按 next_action 停止旧调试，再按 then 在同一流程的当前组件前重新停靠。不要对旧进程反复 next，也不要重建应用。
designer 画布有未保存改动时，写入、reload_editor、run_component、run 和 debug_start 返回 editor_unsaved_changes；先 save_editor(project_ref, expected_revision, dry_run=false, allow_write=true) 保存并复核 revision，再重试；reload_editor 不会丢弃未保存改动。
只有写入、捕获和电脑控制需要 allow_write=true；dry_run 不落盘、不控制电脑。登录或挑战需要用户处理。不保存凭据。
静态通过和工具已接收都不是运行成功；DataCollectionV5 绑定后必须调用 validate_step，它会对当前真实页面执行原生预览，只有 data_row_count>0 才能继续运行，0 行时禁止原样重试。用户要求执行完整图时，调用 run/status 与实际输出验收，run 内部执行项目诊断。纯文件/表格任务不必重复单步调试完整图；网页任务须先取得上述设计态页面证据。失败修复同一流程，结束或失败时停止所拥有的调试/捕获会话。
浏览器生命周期：当 WebBrowser_CreateV3 的 openNewTab=true 时，计划末尾必须使用 WebBrowser_CloseWeb_V2，colse_type=select 且 web 引用上游“网页对象”；MCP 会在缺失时自动追加该原生清理步骤。openNewTab=false 表示复用用户页面，禁止自动关闭。运行结果要检查 browser_cleanup 和关闭步骤状态。
新增组件用 add_step，可传 before_step_id 插入主流程；追加后按 verification_gate 绑定、验证当前组件。精简工具集支持创建、追加、主流程插入、参数编辑与按 revision 删除步骤。完整工具集另有 Agent graph；重排仅 CLI 提供，嵌套控制流编辑未支持，不能伪造已完成。"""
