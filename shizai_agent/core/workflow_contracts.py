"""Small, model-visible authoring contracts; no business planning or site recipes."""

from typing import Any


def rpa_plan_schema(*, for_creation: bool = False) -> dict[str, Any]:
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "description": "模型依据当前请求和实时组件契约编排；config 传语义值，不重复编码原生引号。",
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
                        "depends_on": strings, "inputs": strings, "outputs": strings,
                        "element_ids": strings, "operation_ids": strings,
                        "element_role": {"type": "string"},
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
    "shizai_rpa_analyze_request", "shizai_rpa_component_catalog", "shizai_rpa_validate_plan",
    "shizai_rpa_start_design", "shizai_rpa_read_flow", "shizai_rpa_update_step",
    "shizai_rpa_open_browser", "shizai_rpa_capture_windows", "shizai_rpa_scan_page",
    "shizai_rpa_capture_elements", "shizai_extension_start_capture", "shizai_extension_stop_capture",
    "shizai_rpa_browser_session_start", "shizai_rpa_browser_session_stop",
    "shizai_rpa_validate_step", "shizai_rpa_validate_flow", "shizai_rpa_runtime_preflight",
    "shizai_rpa_materialize", "shizai_rpa_debug_capabilities", "shizai_rpa_debug_start",
    "shizai_rpa_debug_status", "shizai_rpa_debug_command", "shizai_rpa_debug_stop",
    "shizai_rpa_run", "shizai_rpa_status", "shizai_rpa_stop",
})

RPA_AUTHORING_INSTRUCTIONS = """你通过共享 CLI 核心设计原生 RPA，业务方案由你依据用户原句和实时组件目录决定。
新建：analyze_request 一次 → component_catalog 按能力查询 → validate_plan → start_design，open_browser=false。
编辑：已知 flow_ref 时先 read_flow(detail=edit)，选择稳定 step_id，再读取该步骤；update_step(dry_run=true) 预览差异，提交时带 expected_revision 和 allow_write=true；冲突后重读，不重建项目。
本工具集可先保存完整流程骨架，但网页任务必须边设计边运行：先取得页面，再补齐当前步骤的元素绑定并单步验证，看到真实页面变化后继续下一步。目录先 summary 筛选，选定 type 后 contract；不要反复拉取全目录。
先按实时契约选创建/获取网页对象的组件；跳转或操作已有对象的组件不能代替对象创建。对象参数用上游输出的变量引用，不能填 chrome、默认变量标签等字符串。保存骨架后立即 open_browser 执行已选导航，再 capture_windows → scan_page；不能只保存导航节点就假定浏览器已经打开。
输入、点击、提交等改变页面状态的组件，绑定后用 debug_start/复用 → debug_command(next) → debug_status 验证当前步骤；查询结果页真正出现后才扫描并绑定采集字段。网页元素必须从当前 DeepCapture 页面捕获，scan_token 只用于同一快照；页面变化后重扫，仅明确替换旧绑定时 allow_rebind=true。官方拾取用 extension_start_capture，读取会话用 browser_session_start。
调试启动后若修改参数或绑定，旧进程不会热加载修改；返回 requires_restart 时按 next_action 停止旧调试，再按 then 在同一流程的当前组件前重新停靠。不要对旧进程反复 next，也不要重建应用。
只有写入、捕获和电脑控制需要 allow_write=true；dry_run 不落盘、不控制电脑。登录或挑战需要用户处理。不保存凭据。
静态通过和工具已接收都不是运行成功；用户要求执行完整图时，调用 run/status 与实际输出验收，run 内部执行项目诊断。纯文件/表格任务不必重复单步调试完整图；网页任务须先取得上述设计态页面证据。失败修复同一流程，结束或失败时停止所拥有的调试/捕获会话。
精简工具集支持原生 RPA 创建和主流程节点参数编辑。完整工具集另有 Agent graph 与追加节点；主流程重排仅 CLI 提供，MCP 尚无插入、删除、重排接口，嵌套控制流编辑亦未支持，不能伪造已完成。"""
