import { useRef, useState } from "react";
import { Button, Kbd, ListBox, Modal, SearchField } from "@heroui/react";
import { Icon } from "./icons";
import type { Project, Session } from "./types";
import { dateLabel } from "./ui";
import "./chatSearchDialog.css";

interface ChatSearchDialogProps {
  open: boolean;
  onClose: () => void;
  sessions: readonly Session[];
  projects: readonly Project[];
  onSelect: (id: string) => void;
  hasMore: boolean;
  loading: boolean;
  onLoadMore: () => void;
}

export default function ChatSearchDialog({
  open,
  onClose,
  ...content
}: ChatSearchDialogProps) {
  return (
    <Modal.Backdrop
      className="chat-search-backdrop"
      isOpen={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      isDismissable
    >
      <Modal.Container
        className="chat-search-container"
        placement="top"
        size="lg"
      >
        <Modal.Dialog className="chat-search-dialog">
          <ChatSearchContent {...content} onClose={onClose} />
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  );
}

function ChatSearchContent({
  sessions,
  projects,
  onSelect,
  hasMore,
  loading,
  onLoadMore,
  onClose,
}: Omit<ChatSearchDialogProps, "open">) {
  const [query, setQuery] = useState("");
  const resultList = useRef<HTMLDivElement>(null);
  const searchTerm = query.trim().toLocaleLowerCase();
  const options = sessions.filter((session) => {
    if (
      session.archived_at != null ||
      session.deleted_at != null ||
      session.lineage?.kind === "side_chat"
    )
      return false;
    return (
      (session.title || "新对话").toLocaleLowerCase().includes(searchTerm) ||
      session.cwd.toLocaleLowerCase().includes(searchTerm)
    );
  });
  const projectNames = new Map(
    projects.map((project) => [project.id, project.name]),
  );

  return (
    <div
      className="chat-search-content"
      onKeyDownCapture={(event) => {
        if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
    >
      <Modal.Header className="chat-search-header">
        <Modal.Heading>搜索对话</Modal.Heading>
        <Button
          size="sm"
          variant="ghost"
          isIconOnly
          aria-label="关闭搜索"
          onPress={onClose}
        >
          <Icon name="close" size={16} />
        </Button>
      </Modal.Header>

      <SearchField
        className="chat-search-query"
        aria-label="搜索对话标题或目录"
        value={query}
        onChange={setQuery}
        fullWidth
      >
        <SearchField.Group>
          <SearchField.SearchIcon />
          <SearchField.Input
            autoFocus
            spellCheck={false}
            placeholder="搜索对话标题或目录"
            onKeyDownCapture={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter" && options[0]) {
                event.preventDefault();
                onSelect(options[0].id);
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                const items =
                  resultList.current?.querySelectorAll<HTMLElement>(
                    '[role="option"]',
                  );
                const next =
                  event.key === "ArrowDown"
                    ? items?.[0]
                    : items?.[items.length - 1];
                if (next) {
                  event.preventDefault();
                  next.focus();
                }
              }
            }}
          />
          <SearchField.ClearButton aria-label="清除搜索" />
        </SearchField.Group>
      </SearchField>

      <div className="chat-search-results" aria-busy={loading}>
        {options.length > 0 ? (
          <>
            <p className="chat-search-result-label">
              {searchTerm ? "搜索结果" : "最近对话"}
            </p>
            <ListBox
              ref={resultList}
              aria-label="对话结果"
              selectionMode="none"
              items={options}
            >
              {(session) => {
                const title = session.title || "新对话";
                const project =
                  session.project_id && projectNames.get(session.project_id);
                const context = [project, session.cwd]
                  .filter(Boolean)
                  .join(" · ");
                return (
                  <ListBox.Item
                    id={session.id}
                    aria-label={title}
                    aria-description={context}
                    textValue={title}
                    className="chat-search-option"
                    onAction={() => onSelect(session.id)}
                  >
                    <span className="chat-search-option-icon">
                      <Icon name="chat" size={16} />
                    </span>
                    <span className="chat-search-option-copy">
                      <strong>{title}</strong>
                      <small title={context}>{context}</small>
                    </span>
                    <span className="chat-search-option-time">
                      {dateLabel(session.updated_at)}
                    </span>
                    <Icon name="chevron" size={14} />
                  </ListBox.Item>
                );
              }}
            </ListBox>
          </>
        ) : (
          <div className="chat-search-empty" role="status">
            <Icon name="search" size={21} />
            {loading
              ? "正在加载对话…"
              : searchTerm
                ? "没有找到匹配的对话"
                : "暂无对话"}
          </div>
        )}
      </div>

      <Modal.Footer className="chat-search-footer">
        <div className="chat-search-shortcuts" aria-hidden="true">
          <span>
            <Kbd>
              <Kbd.Content>↑</Kbd.Content>
            </Kbd>
            <Kbd>
              <Kbd.Content>↓</Kbd.Content>
            </Kbd>{" "}
            选择
          </span>
          <span>
            <Kbd>
              <Kbd.Content>Enter</Kbd.Content>
            </Kbd>{" "}
            打开
          </span>
          <span>
            <Kbd>
              <Kbd.Content>Esc</Kbd.Content>
            </Kbd>{" "}
            关闭
          </span>
        </div>
        {hasMore && (
          <Button
            size="sm"
            variant="ghost"
            isDisabled={loading}
            onPress={onLoadMore}
          >
            {loading ? "正在加载…" : "加载更早的对话"}
          </Button>
        )}
      </Modal.Footer>
    </div>
  );
}
