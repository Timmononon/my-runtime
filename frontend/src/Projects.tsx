import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { post, readableError, request } from "./api";
import { Icon } from "./icons";
import { Empty, Modal, Popup, Spinner } from "./ui";
import type { Project } from "./types";
type FolderResult = {
  path: string;
  parent: string | null;
  entries: { name: string; path: string }[];
  truncated: boolean;
  roots: { name: string; path: string }[];
};
export default function Projects({
  projects,
  selected,
  onSelect,
  onChanged,
  report,
  onNewChat,
  creatingSession,
  renderSessions,
  searchControl,
  searchField,
}: {
  projects: Project[];
  selected: string | null;
  onSelect: (project: Project | null) => void;
  onChanged: (projects: Project[]) => void;
  report: (error: unknown) => void;
  onNewChat: (project: Project) => void;
  creatingSession: string | null;
  renderSessions: (project: Project | null) => ReactNode;
  searchControl: ReactNode;
  searchField: ReactNode;
}) {
  const folderRequest = useRef(0),
    operation = useRef(false);
  const [menu, setMenu] = useState<string | null>(null),
    [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null),
    [edit, setEdit] = useState<Project | "new" | null>(null),
    [name, setName] = useState(""),
    [cwd, setCwd] = useState(""),
    [busy, setBusy] = useState(false),
    [browsing, setBrowsing] = useState(false),
    [folders, setFolders] = useState<FolderResult | null>(null),
    [folderInput, setFolderInput] = useState(""),
    [folderLoading, setFolderLoading] = useState(false),
    [folderError, setFolderError] = useState(""),
    [removing, setRemoving] = useState<Project | null>(null),
    [operationError, setOperationError] = useState("");
  const refresh = async () => {
    const result = await request<{ projects: Project[] }>(
      "/workbench/projects",
    );
    onChanged(result.projects);
  };
  const start = (project: Project | "new") => {
    setOperationError("");
    setMenu(null);
    setEdit(project);
    setName(project === "new" ? "" : project.name);
    setCwd(project === "new" ? "" : project.cwd);
  };
  const startRemoval = (project: Project) => {
    setMenu(null);
    setMenuAnchor(null);
    setOperationError("");
    setRemoving(project);
  };
  const save = async () => {
    if (operation.current) return;
    operation.current = true;
    setOperationError("");
    setBusy(true);
    try {
      const body = { name: name.trim(), cwd: cwd.trim() };
      const result =
        edit === "new"
          ? await post<Project>("/workbench/projects", body)
          : await request<Project>(
              `/workbench/projects/${encodeURIComponent((edit as Project).id)}`,
              { method: "PATCH", body: JSON.stringify(body) },
            );
      await refresh();
      setEdit(null);
      if (edit === "new") onSelect(result);
    } catch (e) {
      setOperationError(readableError(e));
      report(e);
    } finally {
      operation.current = false;
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!removing || operation.current) return;
    operation.current = true;
    setOperationError("");
    setBusy(true);
    try {
      const result = await request<{ removed: boolean; permanent: boolean }>(
        `/workbench/projects/${encodeURIComponent(removing.id)}?permanent=true`,
        {
          method: "DELETE",
        },
      );
      if (result.removed !== true || result.permanent !== true)
        throw new Error("服务未确认删除项目，请重新载入后核对。");
      await refresh();
      setRemoving(null);
    } catch (e) {
      setOperationError(readableError(e));
      report(e);
    } finally {
      operation.current = false;
      setBusy(false);
    }
  };
  const browse = async (path?: string) => {
    const requestId = ++folderRequest.current;
    setFolderLoading(true);
    setFolderError("");
    try {
      const result = await request<FolderResult>(
        "/workbench/folders" +
          (path ? `?path=${encodeURIComponent(path)}` : ""),
      );
      if (requestId === folderRequest.current) {
        setFolders(result);
        setFolderInput(result.path);
      }
    } catch (error) {
      if (requestId === folderRequest.current)
        setFolderError(error instanceof Error ? error.message : "目录无法读取");
    } finally {
      if (requestId === folderRequest.current) setFolderLoading(false);
    }
  };
  useEffect(() => {
    if (browsing) void browse(cwd || undefined);
  }, [browsing]);
  return (
    <section className="sidebar-projects" aria-label="项目">
      <div className="project-heading">
        <span>项目与对话</span>
        <div className="workspace-header-actions">
          {searchControl}
          <button
            className="icon-button"
            aria-label="创建项目"
            disabled={busy}
            onClick={() => start("new")}
          >
            <Icon name="plus" size={16} />
          </button>
        </div>
      </div>
      {searchField}
      <button
        className={`project-row projectless ${selected === null ? "selected" : ""}`}
        onClick={() => {
          setMenu(null);
          onSelect(null);
        }}
      >
        <Icon name="folder" size={15} />
        <span>独立对话</span>
      </button>
      <div className="project-sessions" aria-label="独立对话列表">
        {renderSessions(null)}
      </div>
      <div className="project-list">
        {projects.map((project) => (
          <div className="project-group" key={project.id}>
            <div
              className={`project-row-wrap ${selected === project.id ? "selected" : ""}`}
            >
              <button
                className="project-row"
                onClick={() => {
                  setMenu(null);
                  onSelect(project);
                }}
                title={project.cwd}
              >
                <Icon name="folder" size={15} />
                <span>{project.name}</span>
              </button>
              <button
                className="icon-button project-new-chat"
                aria-label={`在项目 ${project.name} 中新建对话`}
                title="在此项目中新建对话"
                disabled={busy || !!creatingSession}
                onClick={() => onNewChat(project)}
              >
                {creatingSession === project.id ? (
                  <Spinner />
                ) : (
                  <Icon name="plus" size={16} />
                )}
              </button>
              <button
                className="icon-button project-delete"
                aria-label={`删除项目 ${project.name}`}
                title="删除项目"
                disabled={busy}
                onClick={() => startRemoval(project)}
              >
                <Icon name="trash" size={16} />
              </button>
              <button
                className="icon-button project-more"
                aria-label={`管理项目 ${project.name}`}
                disabled={busy}
                aria-expanded={menu === project.id}
                title="编辑或删除项目"
                onClick={(event) => {
                  setMenuAnchor(event.currentTarget.parentElement);
                  setMenu(menu === project.id ? null : project.id);
                }}
              >
                <Icon name="more" size={16} />
              </button>
              {menu === project.id && (
                <Popup
                  label={`项目 ${project.name} 操作`}
                  onClose={() => setMenu(null)}
                  className="project-popup"
                  anchor={menuAnchor}
                >
                  <button
                    disabled={busy || !!creatingSession}
                    onClick={() => {
                      setMenu(null);
                      setMenuAnchor(null);
                      onNewChat(project);
                    }}
                  >
                    <Icon name="plus" size={15} />
                    新建对话
                  </button>
                  <button onClick={() => start(project)}>
                    <Icon name="settings" size={15} />
                    编辑项目
                  </button>
                  <button onClick={() => startRemoval(project)}>
                    <Icon name="trash" size={15} />
                    删除项目
                  </button>
                </Popup>
              )}
            </div>
            <div
              className="project-sessions"
              aria-label={`项目 ${project.name} 的对话`}
            >
              {renderSessions(project)}
            </div>
          </div>
        ))}
      </div>
      {!projects.length && (
        <p className="project-empty">添加文件夹，让对话有自己的工作范围。</p>
      )}
      {edit && !browsing && (
        <Modal
          title={edit === "new" ? "创建项目" : "编辑项目"}
          onClose={() => {
            if (!busy) setEdit(null);
          }}
        >
          {operationError && (
            <p className="inline-error" role="alert">
              {operationError}
            </p>
          )}
          <p className="modal-description">
            项目连接一个本地文件夹。选择项目后开始的新对话会使用该目录，已有会话的目录不会被改动。
          </p>
          <label className="field">
            项目名称
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="给项目起个名字"
              disabled={busy}
            />
          </label>
          <label className="field">
            本地文件夹
            <div className="folder-input">
              <input
                value={cwd}
                onChange={(e) => setCwd(e.target.value)}
                placeholder="例如 D:\Projects\MyProject"
                disabled={busy}
              />
              <button
                className="button secondary"
                onClick={() => setBrowsing(true)}
                disabled={busy}
              >
                <Icon name="folder" size={15} />
                浏览
              </button>
            </div>
          </label>
          <p className="fine-print">
            可直接输入绝对路径，也可以浏览本机目录。创建项目不会移动或删除文件。
          </p>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setEdit(null)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={busy || !name.trim() || !cwd.trim()}
              onClick={() => void save()}
            >
              {busy ? <Spinner /> : <Icon name="check" size={15} />}保存项目
            </button>
          </div>
        </Modal>
      )}
      {edit && browsing && (
        <Modal title="选择本地文件夹" onClose={() => setBrowsing(false)}>
          <form
            className="folder-location"
            onSubmit={(e) => {
              e.preventDefault();
              void browse(folderInput);
            }}
          >
            <button
              type="button"
              className="icon-button"
              aria-label="上一级目录"
              disabled={folderLoading || !folders?.parent}
              onClick={() => void browse(folders?.parent || undefined)}
            >
              <Icon name="arrow" size={16} />
            </button>
            <input
              aria-label="当前目录路径"
              value={folderInput}
              onChange={(e) => setFolderInput(e.target.value)}
            />
            <button
              type="submit"
              className="button secondary"
              disabled={folderLoading}
            >
              前往
            </button>
          </form>
          <div className="folder-roots">
            {folders?.roots.map((root) => (
              <button key={root.path} onClick={() => void browse(root.path)}>
                <Icon name="folder" size={13} />
                {root.name}
              </button>
            ))}
          </div>
          {folderError && (
            <p className="inline-error" role="alert">
              {folderError}
            </p>
          )}
          <div className="folder-list">
            {folderLoading ? (
              <div className="loading-history">
                <Spinner />
                读取目录
              </div>
            ) : folders?.entries.length ? (
              folders.entries.map((folder) => (
                <button
                  key={folder.path}
                  onClick={() => void browse(folder.path)}
                >
                  <Icon name="folder" size={18} />
                  <span>{folder.name}</span>
                  <Icon name="chevron" size={13} />
                </button>
              ))
            ) : (
              <Empty
                icon="folder"
                title="没有子文件夹"
                detail="仍可选择当前目录。"
              />
            )}
          </div>
          {folders?.truncated && (
            <p className="fine-print">
              子目录较多，仅显示前 200 项；可输入完整路径。
            </p>
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setBrowsing(false)}
            >
              返回
            </button>
            <button
              className="button primary"
              disabled={folderLoading || !folders?.path || !!folderError}
              onClick={() => {
                setCwd(folders!.path);
                setBrowsing(false);
              }}
            >
              选择此文件夹
            </button>
          </div>
        </Modal>
      )}
      {removing && (
        <Modal
          title="删除项目"
          onClose={() => {
            if (!busy) setRemoving(null);
          }}
        >
          {operationError && (
            <p className="inline-error" role="alert">
              {operationError}
            </p>
          )}
          <p className="modal-description">
            删除项目「{removing.name}
            」后，项目登记将永久移除，无法恢复。已有任务继续运行，已有对话和文件会保留；对话会显示在独立对话列表中，原工作目录不变。
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => setRemoving(null)}
            >
              取消
            </button>
            <button
              className="button danger"
              title="永久删除项目"
              disabled={busy}
              onClick={() => void remove()}
            >
              {busy ? <Spinner /> : null}确认删除
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
