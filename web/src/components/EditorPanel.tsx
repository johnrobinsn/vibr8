import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { EditorView } from "@codemirror/view";
import { oneDark } from "@codemirror/theme-one-dark";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { markdown } from "@codemirror/lang-markdown";
import { useStore } from "../store.js";
import { api, type TreeNode } from "../api.js";

/** Custom CodeMirror theme that uses cc-* CSS variables for warm light/dark backgrounds */
const warmTheme = EditorView.theme({
  "&": {
    backgroundColor: "var(--color-cc-code-bg)",
    color: "var(--color-cc-code-fg)",
  },
  ".cm-gutters": {
    backgroundColor: "var(--color-cc-code-bg)",
    borderRight: "1px solid var(--color-cc-border)",
    color: "var(--color-cc-muted)",
  },
  ".cm-activeLineGutter": {
    backgroundColor: "var(--color-cc-active)",
  },
  ".cm-activeLine": {
    backgroundColor: "var(--color-cc-active)",
  },
  "&.cm-focused .cm-cursor": {
    borderLeftColor: "var(--color-cc-fg)",
  },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "var(--color-cc-active)",
  },
});

function getLanguageExtension(filename: string) {
  const ext = filename.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "js":
    case "jsx":
    case "mjs":
      return javascript({ jsx: true });
    case "ts":
    case "tsx":
    case "mts":
      return javascript({ jsx: true, typescript: true });
    case "json":
      return json();
    case "py":
      return python();
    case "html":
    case "htm":
    case "svelte":
    case "vue":
      return html();
    case "css":
    case "scss":
      return css();
    case "md":
    case "mdx":
      return markdown();
    default:
      return undefined;
  }
}

const IMAGE_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif",
]);

function isImageFile(filename: string): boolean {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXTENSIONS.has(ext);
}

function FileIcon({ isDir, expanded }: { isDir: boolean; expanded?: boolean }) {
  if (isDir) {
    return expanded ? (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-cc-primary/60 shrink-0">
        <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v1H2V6z" />
        <path fillRule="evenodd" d="M2 9h16v5a2 2 0 01-2 2H4a2 2 0 01-2-2V9z" clipRule="evenodd" />
      </svg>
    ) : (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-cc-primary/60 shrink-0">
        <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-cc-muted shrink-0">
      <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clipRule="evenodd" />
    </svg>
  );
}

/** Check if a tree node or any descendant has been changed */
function hasChangedDescendant(node: TreeNode, changedFiles: Set<string>): boolean {
  if (changedFiles.has(node.path)) return true;
  if (node.children) {
    return node.children.some((child) => hasChangedDescendant(child, changedFiles));
  }
  return false;
}

function FileTreeNode({
  node,
  depth,
  selectedPath,
  onSelect,
  changedFiles,
  renamingPath,
  onRenameCommit,
  onRenameCancel,
  onRequestRename,
}: {
  node: TreeNode;
  depth: number;
  selectedPath: string | null;
  onSelect: (path: string, isDir?: boolean) => void;
  changedFiles: Set<string>;
  renamingPath: string | null;
  onRenameCommit: (oldPath: string, newName: string) => void;
  onRenameCancel: () => void;
  onRequestRename: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 1);
  const isDir = node.type === "directory";
  const isSelected = node.path === selectedPath;
  const isChanged = !isDir && changedFiles.has(node.path);
  const hasChanged = isDir && hasChangedDescendant(node, changedFiles);
  const isRenaming = node.path === renamingPath;
  const inputRef = useRef<HTMLInputElement>(null);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (isRenaming && inputRef.current) {
      inputRef.current.focus();
      // Select name without extension for files
      const name = node.name;
      const dot = name.lastIndexOf(".");
      if (!isDir && dot > 0) {
        inputRef.current.setSelectionRange(0, dot);
      } else {
        inputRef.current.select();
      }
    }
  }, [isRenaming, node.name, isDir]);

  // Auto-expand parent when a child is being renamed
  useEffect(() => {
    if (isDir && renamingPath && renamingPath.startsWith(node.path + "/") && !expanded) {
      setExpanded(true);
    }
  }, [isDir, renamingPath, node.path, expanded]);

  return (
    <div>
      <button
        onClick={() => {
          if (isRenaming) return;
          onSelect(node.path, isDir);
        }}
        onTouchStart={() => {
          longPressTimer.current = setTimeout(() => {
            longPressTimer.current = null;
            onRequestRename(node.path);
          }, 500);
        }}
        onTouchEnd={() => {
          if (longPressTimer.current) {
            clearTimeout(longPressTimer.current);
            longPressTimer.current = null;
          }
        }}
        onTouchMove={() => {
          if (longPressTimer.current) {
            clearTimeout(longPressTimer.current);
            longPressTimer.current = null;
          }
        }}
        onContextMenu={(e) => e.preventDefault()}
        className={`flex items-center gap-2 w-full mx-1 px-2 py-1 text-[13px] rounded-[10px] hover:bg-cc-hover transition-colors cursor-pointer whitespace-nowrap ${
          isSelected ? "bg-cc-active text-cc-fg" : "text-cc-fg/80"
        }`}
        style={{ paddingLeft: `${depth * 12 + 8}px`, width: "calc(100% - 8px)" }}
      >
        {isDir && (
          <svg
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); if (!expanded) onSelect(node.path, true); }}
            viewBox="0 0 20 20"
            fill="currentColor"
            className={`w-4 h-4 p-0.5 text-cc-muted shrink-0 transition-transform cursor-pointer hover:text-cc-fg ${expanded ? "rotate-90" : ""}`}
          >
            <path fillRule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clipRule="evenodd" />
          </svg>
        )}
        {!isDir && <span className="w-3 shrink-0" />}
        <FileIcon isDir={isDir} expanded={expanded} />
        {isRenaming ? (
          <input
            ref={inputRef}
            defaultValue={node.name}
            className="bg-cc-bg text-cc-fg text-[13px] px-1 py-0 border border-cc-primary rounded outline-none w-full"
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                const val = (e.target as HTMLInputElement).value.trim();
                if (val && val !== node.name) onRenameCommit(node.path, val);
                else onRenameCancel();
              } else if (e.key === "Escape") {
                onRenameCancel();
              }
            }}
            onBlur={(e) => {
              const val = e.target.value.trim();
              if (val && val !== node.name) onRenameCommit(node.path, val);
              else onRenameCancel();
            }}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <span className={`leading-snug ${isChanged ? "text-cc-warning" : ""}`}>{node.name}</span>
        )}
        {(isChanged || hasChanged) && (
          <span className="w-1.5 h-1.5 rounded-full bg-cc-warning shrink-0 ml-auto" />
        )}
      </button>
      {isDir && expanded && node.children?.map((child) => (
        <FileTreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          selectedPath={selectedPath}
          onSelect={onSelect}
          changedFiles={changedFiles}
          renamingPath={renamingPath}
          onRenameCommit={onRenameCommit}
          onRenameCancel={onRenameCancel}
          onRequestRename={onRequestRename}
        />
      ))}
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  if (!diff) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-cc-muted text-sm">No changes</p>
      </div>
    );
  }
  const lines = diff.split("\n");
  return (
    <div className="h-full overflow-auto font-mono-code text-[13px] p-4">
      {lines.map((line, i) => {
        let cls = "text-cc-fg/60";
        if (line.startsWith("+") && !line.startsWith("+++")) cls = "bg-cc-success/10 text-cc-success";
        else if (line.startsWith("-") && !line.startsWith("---")) cls = "bg-cc-error/10 text-cc-error";
        else if (line.startsWith("@@")) cls = "text-cc-primary";
        else if (line.startsWith("diff") || line.startsWith("index")) cls = "text-cc-muted";
        return <div key={i} className={`px-2 ${cls}`}>{line || "\u00A0"}</div>;
      })}
    </div>
  );
}

export function EditorPanel({ sessionId }: { sessionId: string }) {
  const darkMode = useStore((s) => s.darkMode);
  const session = useStore((s) => s.sessions.get(sessionId));
  const sdkSession = useStore((s) => s.sdkSessions.find((sdk) => sdk.sessionId === sessionId));
  const openFilePath = useStore((s) => s.editorOpenFile.get(sessionId) ?? null);
  const setEditorOpenFile = useStore((s) => s.setEditorOpenFile);
  const [fileTreeOpen, setFileTreeOpen] = useState(true);
  const [selectedDir, setSelectedDir] = useState<string | null>(null);

  const changedFilesSet = useStore((s) => s.changedFiles.get(sessionId));

  const cwd = session?.cwd || sdkSession?.cwd;

  const [tree, setTree] = useState<TreeNode[]>([]);
  const [treeLoading, setTreeLoading] = useState(false);
  const [fileContent, setFileContent] = useState<string>("");
  const [savedContent, setSavedContent] = useState<string>("");
  const [fileLoading, setFileLoading] = useState(false);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [saveStatus, setSaveStatus] = useState<"saved" | "saving" | "dirty" | null>(null);
  const [diffMode, setDiffMode] = useState(false);
  const [diffContent, setDiffContent] = useState<string>("");
  const [diffLoading, setDiffLoading] = useState(false);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renamingTab, setRenamingTab] = useState(false);
  const tabRenameRef = useRef<HTMLInputElement>(null);
  const tabLongPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const changedFiles = useMemo(() => changedFilesSet ?? new Set<string>(), [changedFilesSet]);

  const relativeChangedFiles = useMemo(() => {
    if (!changedFiles.size || !cwd) return [];
    return [...changedFiles]
      .map((fp) => ({ abs: fp, rel: fp.startsWith(cwd + "/") ? fp.slice(cwd.length + 1) : fp }))
      .sort((a, b) => a.rel.localeCompare(b.rel));
  }, [changedFiles, cwd]);

  const isDirty = fileContent !== savedContent && openFilePath !== null;

  // Load file tree
  useEffect(() => {
    if (!cwd) return;
    setTreeLoading(true);
    api.getFileTree(cwd).then((res) => {
      setTree(res.tree);
      setTreeLoading(false);
    }).catch(() => setTreeLoading(false));
  }, [cwd]);

  // Load file content when a file is selected (skip for images)
  useEffect(() => {
    if (!openFilePath) return;
    const name = openFilePath.split("/").pop() ?? "";
    if (isImageFile(name)) {
      setFileContent("");
      setSavedContent("");
      setFileLoading(false);
      return;
    }
    setFileLoading(true);
    setSaveStatus(null);
    api.readFile(openFilePath).then((res) => {
      setFileContent(res.content);
      setSavedContent(res.content);
      setFileLoading(false);
    }).catch(() => {
      setFileContent("// Error loading file");
      setSavedContent("");
      setFileLoading(false);
    });
  }, [openFilePath]);

  // Track edits without auto-saving
  const handleChange = useCallback((value: string) => {
    setFileContent(value);
    setSaveStatus("dirty");
  }, []);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  // Reset diff mode when file changes
  useEffect(() => { setDiffMode(false); }, [openFilePath]);

  // Fetch diff when diff mode is activated on a changed file
  useEffect(() => {
    if (!diffMode || !openFilePath || !changedFiles.has(openFilePath)) return;
    setDiffLoading(true);
    api.getFileDiff(openFilePath).then((res) => {
      setDiffContent(res.diff);
      setDiffLoading(false);
    }).catch(() => { setDiffContent(""); setDiffLoading(false); });
  }, [diffMode, openFilePath, changedFiles]);

  const handleFileSelect = useCallback((path: string, isDir = false) => {
    if (isDir) {
      setSelectedDir(path);
      return;
    }
    setSelectedDir(null);
    setEditorOpenFile(sessionId, path);
    // Close file tree on mobile after selecting a file
    if (typeof window !== "undefined" && window.innerWidth < 640) {
      setFileTreeOpen(false);
    }
  }, [sessionId, setEditorOpenFile]);

  const refreshTree = useCallback(() => {
    if (!cwd) return Promise.resolve();
    return api.getFileTree(cwd).then((res) => setTree(res.tree)).catch(() => {});
  }, [cwd]);


  const getTargetDir = useCallback(() => {
    // Use explicitly selected directory first
    if (selectedDir) return selectedDir;
    if (!openFilePath) return cwd || "";
    // File selected — use its parent directory
    const lastSlash = openFilePath.lastIndexOf("/");
    return lastSlash > 0 ? openFilePath.slice(0, lastSlash) : cwd || "";
  }, [selectedDir, openFilePath, cwd]);

  const nextUntitledName = useCallback((dirPath: string) => {
    const findChildren = (nodes: TreeNode[], path: string): TreeNode[] => {
      for (const n of nodes) {
        if (n.path === path) return n.children || [];
        if (n.children) {
          const found = findChildren(n.children, path);
          if (found.length) return found;
        }
      }
      return [];
    };
    const siblings = dirPath === cwd ? tree : findChildren(tree, dirPath);
    const names = new Set(siblings.map((n) => n.name.toLowerCase()));
    let i = 1;
    while (names.has(`untitled ${i}`)) i++;
    return `Untitled ${i}`;
  }, [tree, cwd]);

  const handleNewFolder = useCallback(async () => {
    const dir = getTargetDir();
    const name = nextUntitledName(dir);
    const fullPath = `${dir}/${name}`;
    try {
      await api.mkdir(fullPath);
      refreshTree();
      setSelectedDir(fullPath);
      setTimeout(() => setRenamingPath(fullPath), 100);
    } catch (e) {
      console.error("[editor] Failed to create folder:", e);
    }
  }, [getTargetDir, nextUntitledName, refreshTree]);

  const handleNewFile = useCallback(async () => {
    const dir = getTargetDir();
    const name = nextUntitledName(dir);
    const fullPath = `${dir}/${name}`;
    try {
      await api.writeFile(fullPath, "");
      refreshTree();
      setTimeout(() => setRenamingPath(fullPath), 100);
    } catch (e) {
      console.error("[editor] Failed to create file:", e);
    }
  }, [getTargetDir, nextUntitledName, refreshTree]);

  const handleRenameCommit = useCallback(async (oldPath: string, newName: string) => {
    const parentDir = oldPath.substring(0, oldPath.lastIndexOf("/"));
    const newPath = `${parentDir}/${newName}`;
    try {
      await api.rename(oldPath, newPath);
      setRenamingPath(null);
      refreshTree();
      // If the renamed item was open in editor, update the open file
      if (openFilePath === oldPath) {
        handleFileSelect(newPath);
      }
    } catch (e) {
      console.error("[editor] Failed to rename:", e);
      setRenamingPath(null);
    }
  }, [refreshTree, openFilePath, handleFileSelect]);

  const fileName = openFilePath?.split("/").pop() ?? null;

  useEffect(() => {
    if (renamingTab && tabRenameRef.current) {
      tabRenameRef.current.focus();
      const name = fileName || "";
      const dot = name.lastIndexOf(".");
      if (dot > 0) {
        tabRenameRef.current.setSelectionRange(0, dot);
      } else {
        tabRenameRef.current.select();
      }
    }
  }, [renamingTab, fileName]);

  const handleTabRename = useCallback(async (newName: string) => {
    setRenamingTab(false);
    if (!openFilePath || !newName.trim() || newName.trim() === fileName) return;
    const parentDir = openFilePath.substring(0, openFilePath.lastIndexOf("/"));
    const newPath = `${parentDir}/${newName.trim()}`;
    try {
      await api.rename(openFilePath, newPath);
      setEditorOpenFile(sessionId, newPath);
      refreshTree();
    } catch (e) {
      console.error("[editor] Failed to rename:", e);
    }
  }, [openFilePath, fileName, sessionId, setEditorOpenFile, refreshTree]);

  const handleRenameCancel = useCallback(() => {
    setRenamingPath(null);
  }, []);

  const handleDelete = useCallback(async () => {
    if (!openFilePath) return;
    const name = openFilePath.split("/").pop() ?? openFilePath;
    if (!window.confirm(`Delete "${name}"?`)) return;
    try {
      await api.deleteFile(openFilePath);
      setEditorOpenFile(sessionId, "");
      setFileContent("");
      setSavedContent("");
      setSaveStatus(null);
      refreshTree();
    } catch (e) {
      console.error("[editor] Failed to delete:", e);
    }
  }, [openFilePath, sessionId, setEditorOpenFile, refreshTree]);

  const handleClose = useCallback(() => {
    if (isDirty && !window.confirm("Discard unsaved changes?")) return;
    setEditorOpenFile(sessionId, "");
    setFileContent("");
    setSavedContent("");
    setSaveStatus(null);
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
  }, [isDirty, sessionId, setEditorOpenFile]);

  const handleSave = useCallback(() => {
    if (!openFilePath || !isDirty) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    setSaveStatus("saving");
    api.writeFile(openFilePath, fileContent).then(() => {
      setSavedContent(fileContent);
      setSaveStatus("saved");
      setTimeout(() => setSaveStatus(null), 2000);
    }).catch(() => setSaveStatus("dirty"));
  }, [openFilePath, isDirty, fileContent]);

  const handleDiscard = useCallback(() => {
    if (!isDirty) return;
    if (!window.confirm("Discard unsaved changes?")) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    setFileContent(savedContent);
    setSaveStatus(null);
  }, [isDirty, savedContent]);

  const extensions = useMemo(() => {
    const exts = [warmTheme];
    if (darkMode) exts.push(oneDark);
    if (fileName) {
      const lang = getLanguageExtension(fileName);
      if (lang) exts.push(lang);
    }
    return exts;
  }, [fileName, darkMode]);

  if (!cwd) {
    return (
      <div className="flex-1 flex items-center justify-center h-full">
        <p className="text-cc-muted text-sm">Waiting for session to initialize...</p>
      </div>
    );
  }

  return (
    <div className="h-full flex bg-cc-bg relative">
      {/* Mobile backdrop */}
      {fileTreeOpen && (
        <div
          className="fixed inset-0 bg-black/30 z-20 sm:hidden"
          onClick={() => setFileTreeOpen(false)}
        />
      )}

      {/* File Tree — overlay on mobile, inline on desktop */}
      <div className={`
        ${fileTreeOpen ? "w-[85vw] max-w-[320px] sm:max-w-none translate-x-0" : "w-0 -translate-x-full"}
        fixed top-0 left-0 sm:relative sm:top-auto sm:left-auto z-30 sm:z-auto
        ${fileTreeOpen ? "sm:w-[220px] sm:max-w-none" : "sm:w-0 sm:-translate-x-full"}
        shrink-0 h-[100dvh] sm:h-full flex flex-col bg-cc-sidebar border-r border-cc-border transition-all duration-200 overflow-hidden
      `}>
        <div className="w-full px-4 py-3 text-[11px] font-semibold text-cc-fg uppercase tracking-wider border-b border-cc-border shrink-0 flex items-center justify-between">
          <span>Explorer</span>
          <div className="flex items-center gap-1">
            <button
              onClick={handleNewFile}
              title="New File"
              className="w-5 h-5 flex items-center justify-center rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                <path d="M9 2H4.5A1.5 1.5 0 003 3.5v9A1.5 1.5 0 004.5 14h7a1.5 1.5 0 001.5-1.5V6L9 2z" strokeLinejoin="round" />
                <path d="M8 7v4M6 9h4" strokeLinecap="round" />
              </svg>
            </button>
            <button
              onClick={handleNewFolder}
              title="New Folder"
              className="w-5 h-5 flex items-center justify-center rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                <path d="M2 4.5A1.5 1.5 0 013.5 3H6l1.5 1.5h5A1.5 1.5 0 0114 6v5.5a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 11.5v-7z" strokeLinejoin="round" />
                <path d="M8 7v4M6 9h4" strokeLinecap="round" />
              </svg>
            </button>
            {openFilePath && (
              <button
                onClick={handleDelete}
                title="Delete selected"
                className="w-5 h-5 flex items-center justify-center rounded-md text-cc-muted hover:text-red-400 hover:bg-cc-hover transition-colors cursor-pointer"
              >
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                  <path d="M3 4h10M6 4V3h4v1M5 4v8.5a.5.5 0 00.5.5h5a.5.5 0 00.5-.5V4" strokeLinejoin="round" />
                </svg>
              </button>
            )}
            <button
              onClick={refreshTree}
              title="Refresh"
              className="w-5 h-5 flex items-center justify-center rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                <path d="M13 8A5 5 0 113.5 5.5" strokeLinecap="round" />
                <path d="M3 2v4h4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <button
              onClick={() => setFileTreeOpen(false)}
              className="w-5 h-5 flex items-center justify-center rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer sm:hidden"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" className="w-3 h-3">
                <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </div>

        {/* Changed Files section */}
        {relativeChangedFiles.length > 0 && (
          <div className="border-b border-cc-border">
            <div className="px-4 py-2 text-[11px] font-semibold text-cc-fg uppercase tracking-wider flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-cc-warning" />
              Changed ({relativeChangedFiles.length})
            </div>
            <div className="max-h-[120px] overflow-y-auto pb-1">
              {relativeChangedFiles.map(({ abs, rel }) => (
                <button
                  key={abs}
                  onClick={() => handleFileSelect(abs)}
                  className={`flex items-center gap-2 w-full mx-1 px-2 py-1 text-[13px] rounded-[10px] hover:bg-cc-hover transition-colors cursor-pointer whitespace-nowrap ${
                    abs === openFilePath ? "bg-cc-active text-cc-fg" : "text-cc-fg/70"
                  }`}
                  style={{ width: "calc(100% - 8px)" }}
                >
                  <svg viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 text-cc-warning shrink-0">
                    <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clipRule="evenodd" />
                  </svg>
                  <span className="truncate leading-snug">{rel}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden pt-1 pb-[max(env(safe-area-inset-bottom,0px),16px)]">
          {treeLoading ? (
            <div className="flex items-center justify-center py-8">
              <div className="w-4 h-4 border-2 border-cc-primary border-t-transparent rounded-full animate-spin" />
            </div>
          ) : tree.length === 0 ? (
            <p className="text-cc-muted text-xs px-4 py-4">No files found</p>
          ) : (
            tree.map((node) => (
              <FileTreeNode
                key={node.path}
                node={node}
                depth={0}
                selectedPath={selectedDir || openFilePath}
                onSelect={handleFileSelect}
                changedFiles={changedFiles}
                renamingPath={renamingPath}
                onRenameCommit={handleRenameCommit}
                onRenameCancel={handleRenameCancel}
                onRequestRename={setRenamingPath}
              />
            ))
          )}
        </div>
      </div>

      {/* Editor area — flexible width */}
      <div className="flex-1 min-w-0 h-full flex flex-col">
        {/* Tab bar */}
        {openFilePath && (
          <div className="shrink-0 flex items-center gap-2 sm:gap-2.5 px-2 sm:px-4 py-2.5 bg-cc-card border-b border-cc-border">
            {/* File tree toggle button */}
            {!fileTreeOpen && (
              <button
                onClick={() => setFileTreeOpen(true)}
                className="flex items-center justify-center w-6 h-6 rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer shrink-0"
                title="Show file tree"
              >
                <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5">
                  <path d="M1 3.5A1.5 1.5 0 012.5 2h3.379a1.5 1.5 0 011.06.44l.622.621a.5.5 0 00.353.146H13.5A1.5 1.5 0 0115 4.707V12.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 011 12.5v-9z" />
                </svg>
              </button>
            )}
            <div className="flex items-center gap-2 min-w-0">
              {isDirty && <span className="w-2 h-2 rounded-full bg-cc-warning shrink-0" title="Unsaved changes" />}
              {renamingTab ? (
                <input
                  ref={tabRenameRef}
                  defaultValue={fileName || ""}
                  className="bg-cc-bg text-cc-fg text-[13px] font-medium px-1 py-0 border border-cc-primary rounded outline-none min-w-[80px]"
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleTabRename((e.target as HTMLInputElement).value);
                    else if (e.key === "Escape") setRenamingTab(false);
                  }}
                  onBlur={(e) => handleTabRename(e.target.value)}
                />
              ) : (
                <span
                  className="text-cc-fg text-[13px] font-medium truncate select-none"
                  onTouchStart={() => {
                    tabLongPressTimer.current = setTimeout(() => {
                      tabLongPressTimer.current = null;
                      setRenamingTab(true);
                    }, 500);
                  }}
                  onTouchEnd={() => {
                    if (tabLongPressTimer.current) {
                      clearTimeout(tabLongPressTimer.current);
                      tabLongPressTimer.current = null;
                    }
                  }}
                  onTouchMove={() => {
                    if (tabLongPressTimer.current) {
                      clearTimeout(tabLongPressTimer.current);
                      tabLongPressTimer.current = null;
                    }
                  }}
                  onDoubleClick={() => setRenamingTab(true)}
                >{fileName}</span>
              )}
            </div>
            <span className="text-cc-muted truncate text-[11px] hidden sm:inline">{openFilePath}</span>
            {saveStatus === "saving" && <span className="text-cc-muted text-[11px] shrink-0">Saving...</span>}
            {saveStatus === "saved" && <span className="text-cc-success text-[11px] shrink-0">Saved</span>}
            <div className="flex items-center gap-1 ml-auto shrink-0">
              {isDirty && (
                <>
                  <button
                    onClick={handleSave}
                    className="px-2 py-1 text-[11px] font-medium rounded-md text-cc-fg bg-cc-primary/20 hover:bg-cc-primary/30 transition-colors cursor-pointer"
                    title="Save"
                  >
                    Save
                  </button>
                  <button
                    onClick={handleDiscard}
                    className="px-2 py-1 text-[11px] font-medium rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                    title="Discard changes"
                  >
                    Discard
                  </button>
                </>
              )}
              {changedFiles.has(openFilePath) && (
                <div className="flex items-center bg-cc-hover rounded-lg p-0.5">
                  <button
                    onClick={() => setDiffMode(false)}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors ${
                      !diffMode ? "bg-cc-card text-cc-fg shadow-sm" : "text-cc-muted hover:text-cc-fg"
                    }`}
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => setDiffMode(true)}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors ${
                      diffMode ? "bg-cc-card text-cc-fg shadow-sm" : "text-cc-muted hover:text-cc-fg"
                    }`}
                  >
                    Diff
                  </button>
                </div>
              )}
              <button
                onClick={handleClose}
                className="w-6 h-6 flex items-center justify-center rounded-md text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                title="Close file"
              >
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" className="w-3 h-3">
                  <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
                </svg>
              </button>
            </div>
          </div>
        )}

        {/* Editor content */}
        <div className="flex-1 overflow-hidden">
          {!openFilePath ? (
            <div className="h-full flex flex-col items-center justify-center">
              {!fileTreeOpen && (
                <button
                  onClick={() => setFileTreeOpen(true)}
                  className="mb-4 flex items-center gap-2 px-3 py-2 rounded-lg text-xs text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
                >
                  <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5">
                    <path d="M1 3.5A1.5 1.5 0 012.5 2h3.379a1.5 1.5 0 011.06.44l.622.621a.5.5 0 00.353.146H13.5A1.5 1.5 0 0115 4.707V12.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 011 12.5v-9z" />
                  </svg>
                  Show file tree
                </button>
              )}
              <div className="text-center space-y-3">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" className="w-12 h-12 text-cc-muted/30 mx-auto" strokeWidth={1}>
                  <path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z" />
                  <polyline points="13,2 13,9 20,9" />
                </svg>
                <p className="text-cc-muted text-sm">Select a file to edit</p>
              </div>
            </div>
          ) : fileLoading || diffLoading ? (
            <div className="h-full flex items-center justify-center">
              <div className="w-5 h-5 border-2 border-cc-primary border-t-transparent rounded-full animate-spin" />
            </div>
          ) : diffMode && changedFiles.has(openFilePath) ? (
            <DiffView diff={diffContent} />
          ) : fileName && isImageFile(fileName) ? (
            <div className="h-full flex items-center justify-center p-8 overflow-auto bg-[repeating-conic-gradient(var(--color-cc-border)_0%_25%,transparent_0%_50%)_0_0/20px_20px]">
              <img
                src={`/api/fs/raw?path=${encodeURIComponent(openFilePath)}`}
                alt={fileName}
                className="max-w-full max-h-full object-contain rounded shadow-lg"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = "none";
                  (e.target as HTMLImageElement).parentElement!.innerHTML =
                    '<p class="text-cc-muted text-sm">Failed to load image</p>';
                }}
              />
            </div>
          ) : (
            <CodeMirror
              value={fileContent}
              onChange={handleChange}
              extensions={extensions}
              theme={darkMode ? "dark" : "light"}
              height="100%"
              className="h-full text-[13px]"
              basicSetup={{
                lineNumbers: true,
                foldGutter: true,
                highlightActiveLine: true,
                autocompletion: true,
                bracketMatching: true,
                indentOnInput: true,
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
