import type { ChatMessage } from "./types";

export interface ToolErrorSummary {
  title: string;
  detail: string;
}

// Tool output can contain source text, process output or remote response bodies.
// Render only these local explanations and validated status codes in the chat;
// arguments, raw output and provider reasoning are never error-detail markup.
export function toolErrorSummary(
  message: ChatMessage | undefined,
  tool: string,
): ToolErrorSummary | null {
  if (!message) return null;
  const metadata = message.metadata;
  if (metadata.outcome === "unknown")
    return {
      title: "执行结果待核验",
      detail: "操作可能已经发生，请先核验原执行记录，再决定下一步。",
    };
  if (!metadata.is_error) return null;
  const category = metadata.error_category;
  const content = message.content;
  const data = metadata.data;
  const reason =
    data && typeof data === "object" && "reason" in data ? data.reason : null;

  if (category === "permission")
    return {
      title: "操作未获授权",
      detail: content.includes("no attended approval channel")
        ? "此操作需要批准，但本次运行没有可用的审批通道。请在任务待办中检查。"
        : "请在任务待办中查看批准请求，或检查当前会话的权限和工作目录范围。",
    };
  if (
    tool.startsWith("github_") &&
    (reason === "source_not_found" ||
      reason === "source_ambiguous" ||
      reason === "source_unavailable" ||
      (category === "validation" &&
        content.includes("GitHub action is unavailable")))
  )
    return {
      title: "GitHub 来源或任务绑定不可用",
      detail:
        reason === "source_ambiguous"
          ? "当前会话有多个 GitHub 来源，请先选择本任务对应的来源。"
          : reason === "source_unavailable"
            ? "当前会话没有可用的 GitHub 来源，请检查来源配置和任务绑定。"
            : "请使用当前任务绑定的 GitHub 来源 ID；仓库名和 Issue ID 不能代替来源 ID。",
    };
  if (category === "validation")
    return {
      title: "工具参数校验未通过",
      detail: "参数不符合此工具的要求，请按工具定义检查字段、类型和必填项。",
    };
  if (category === "http_status") {
    const found = content.match(/^HTTP:\s*(\d{3})\s*$/m)?.[1];
    const status =
      found && Number(found) >= 100 && Number(found) <= 599 ? found : null;
    return {
      title: status ? `远端返回 HTTP ${status}` : "远端请求未成功",
      detail:
        status === "404" && content.includes('"This repository is empty."')
          ? "GitHub 仓库尚无已提交内容，读取文件列表会返回 404。"
          : status === "404"
            ? "请求的资源不存在，或当前身份无权访问。请检查目标地址和仓库权限。"
            : "请检查目标资源、访问权限和服务状态后再继续。",
    };
  }
  if (category === "io")
    return {
      title: "文件或目录读取失败",
      detail:
        content.includes("[WinError 2]") ||
        content.includes("No such file or directory")
          ? "文件或目录不存在，请先确认路径和本地仓库内容。"
          : "请检查文件是否存在、是否可读，以及是否在授权工作目录内。",
    };
  if (category === "unavailable" || category === "configuration")
    return {
      title: "工具配置需要检查",
      detail:
        tool === "web_search"
          ? "请在设置中检查搜索服务是否启用、提供商是否已配置。"
          : "工具或关联服务当前不可用，请检查已保存的配置和连接。",
    };
  if (category === "timeout")
    return {
      title: "工具执行超时",
      detail: "请检查原执行记录和时间预算，确认结果后再决定是否重试。",
    };
  if (category === "cancelled")
    return {
      title: "工具执行已取消",
      detail: "本次操作已停止，请先确认已完成的部分。",
    };
  if (category === "process") {
    const exitCode =
      data && typeof data === "object" && "exit_code" in data
        ? data.exit_code
        : undefined;
    return {
      title:
        typeof exitCode === "number" && Number.isInteger(exitCode)
          ? `命令执行失败（退出码 ${exitCode}）`
          : "命令执行失败",
      detail: "请检查命令是否适用于当前执行环境，以及工作目录和程序是否可用。",
    };
  }
  return {
    title: "工具调用未完成",
    detail: "请检查任务待办、相关配置及原执行记录，确认原因后再继续。",
  };
}
