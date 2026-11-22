import { createElement, useEffect, useRef, useState, type ReactNode } from "react";
import MarkdownIt, { type Token } from "markdown-it";
import ContentCopyOutlined from "@mui/icons-material/ContentCopyOutlined";
import ImageNotSupportedOutlined from "@mui/icons-material/ImageNotSupportedOutlined";
import OpenInNewOutlined from "@mui/icons-material/OpenInNewOutlined";
import { t } from "../copy";
import { resolveNoteLink, type NoteLink } from "./noteLinks";

const markdown = new MarkdownIt({ html: false, linkify: false });

markdown.inline.ruler.before("link", "wikilink", (state, silent) => {
  const start = state.pos;
  const embedding = state.src[start] === "!";
  const open = start + Number(embedding);
  if (!state.src.startsWith("[[", open)) return false;
  const end = state.src.indexOf("]]", open + 2);
  if (end < 0 || end + 2 > state.posMax) return false;
  const content = state.src.slice(open + 2, end);
  const alias = content.indexOf("|");
  const target = (alias < 0 ? content : content.slice(0, alias)).trim();
  if (!target || /[\n\r\[\]]/u.test(content)) return false;
  if (!silent) {
    const token = state.push(embedding ? "wiki_image" : "wikilink", "", 0);
    token.content = alias < 0 ? target : content.slice(alias + 1);
    token.attrSet("href", target);
  }
  state.pos = end + 2;
  return true;
});

function CodeBlock({ code, language }: { code: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setFailed(false);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      setFailed(true);
    }
  }

  return <div className="md-code-block">
    <div className="md-code-toolbar">
      <span>{language}</span>
      <button type="button" title={t("common.copy")} aria-label={t("common.copy")} onClick={() => void copy()}>
        <ContentCopyOutlined />{copied ? t("common.copied") : failed ? t("common.copyFailed") : null}
      </button>
    </div>
    <pre><code>{code}</code></pre>
  </div>;
}

const plainText = (tokens: Token[]): string => tokens.map((token) => token.children ? plainText(token.children) : token.content).join("");

const normalizeHeading = (text: string) => text.trim().normalize("NFC");
const headingSlug = (text: string) => normalizeHeading(text).toLocaleLowerCase().replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s+/gu, "-");

export function findNoteHeading<T extends Pick<HTMLElement, "dataset" | "tagName">>(headings: T[], heading: string): T | undefined {
  const stack: { level: number; title: string }[] = [];
  const sections = headings.map((element) => {
    const level = Number(element.tagName.slice(1));
    const title = normalizeHeading(element.dataset.heading ?? "");
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    stack.push({ level, title });
    return { element, title, breadcrumb: stack.map((part) => part.title).join(" / ") };
  });
  const wanted = normalizeHeading(heading);
  const leaf = wanted.split(" / ").at(-1) ?? wanted;
  const slug = headingSlug(wanted);
  const leafSlug = headingSlug(leaf);
  return (sections.find((section) => section.breadcrumb === wanted) ??
    sections.find((section) => section.title === wanted) ??
    sections.find((section) => headingSlug(section.breadcrumb) === slug) ??
    sections.find((section) => section.title === leaf) ??
    sections.find((section) => headingSlug(section.title) === leafSlug))?.element;
}

export function renderNoteMarkdown(body: string, currentPath: string, paths: string[], onLink: (link: NoteLink) => void): ReactNode[] {
  const renderLink = (href: string, text: ReactNode, key: number, wiki = false) => {
    const link = resolveNoteLink(href, currentPath, paths, wiki);
    if (!link) return <span key={key}>{text}</span>;
    return <button type="button" className={`md-link md-link-${link.kind}`} key={key} onClick={() => onLink(link)}>
      {text}{link.kind === "external" && <OpenInNewOutlined />}
    </button>;
  };

  function render(tokens: Token[]): ReactNode[] {
    const stack: ReactNode[][] = [[]];
    const openings: { token: Token; index: number }[] = [];
    tokens.forEach((token, index) => {
      if (token.nesting === 1) {
        openings.push({ token, index });
        stack.push([]);
        return;
      }
      let node: ReactNode;
      if (token.nesting === -1) {
        const entry = openings.pop()!;
        const children = stack.pop()!;
        const open = entry.token;
        if (open.hidden) node = children;
        else if (open.type === "link_open") node = renderLink(String(open.attrGet("href") ?? ""), children, index);
        else {
          const props: { key: number; start?: number; "data-heading"?: string } = { key: index };
          if (open.tag === "ol" && open.attrGet("start")) props.start = Number(open.attrGet("start"));
          if (open.type === "heading_open") props["data-heading"] = plainText(tokens[entry.index + 1].children ?? []);
          node = createElement(open.tag, props, children);
          if (open.tag === "table") node = <div key={index} className="md-table">{node}</div>;
        }
      } else {
        switch (token.type) {
          case "inline": {
            const children = token.children ?? [];
            const listStart = tokens[index - 2]?.type === "list_item_open";
            const first = children[0];
            const task = listStart && first?.type === "text" ? /^\[([ xX])\] /.exec(first.content) : null;
            if (!task) node = render(children);
            else {
              first.content = first.content.slice(task[0].length);
              node = <span key={index}><input type="checkbox" disabled checked={task[1] !== " "} />{render(children)}</span>;
            }
            break;
          }
          case "text":
            node = token.content.split(/(<\/?[A-Za-z][^<>]*>)/gu).map((part, partIndex) =>
              partIndex % 2 ? <code key={partIndex}>{part}</code> : part);
            break;
          case "code_inline":
            node = <code key={index}>{token.content}</code>;
            break;
          case "fence":
          case "code_block":
            node = <CodeBlock key={index} code={token.content} language={token.info.trim().split(/\s+/u)[0]} />;
            break;
          case "wikilink":
            node = openings.some(({ token: open }) => open.type === "link_open")
              ? token.content : renderLink(String(token.attrGet("href") ?? ""), token.content, index, true);
            break;
          case "image":
          case "wiki_image": {
            const alt = token.children ? plainText(token.children) : token.content;
            node = <span key={index} className="md-image"><ImageNotSupportedOutlined />{t("note.imageHidden")}{alt && ` · ${alt}`}</span>;
            break;
          }
          case "softbreak":
            node = "\n";
            break;
          case "hardbreak":
            node = <br key={index} />;
            break;
          case "hr":
            node = <hr key={index} />;
            break;
          default:
            node = null;
        }
      }
      stack[stack.length - 1].push(node);
    });
    return stack[0];
  }

  return render(markdown.parse(body, {}));
}
