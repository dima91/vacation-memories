// A small HTML reader for the output of `react-dom/server`, enough to check the accessibility of what the components
// render without a browser: it is well-formed (React escapes `<` and `>` in attributes) and has no scripts.
export interface HtmlNode {
  tag: string;
  attrs: Record<string, string>;
  children: HtmlNode[];
  text: string; // for the text nodes (tag "#text")
  parent: HtmlNode | null;
}

const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
const decode = (text: string): string => text.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

export function parseHtml(html: string): HtmlNode {
  const root: HtmlNode = { tag: "#root", attrs: {}, children: [], text: "", parent: null };
  let current = root;
  const token = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w:-]*)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  for (const found of html.matchAll(token)) {
    const [whole, closing, tag, attributes, selfClosing, text] = found;
    if (whole.startsWith("<!--")) continue;
    if (text !== undefined) {
      current.children.push({ tag: "#text", attrs: {}, children: [], text: decode(text), parent: current });
    } else if (closing) {
      let open: HtmlNode | null = current;
      while (open && open.tag !== tag) open = open.parent;
      if (open?.parent) current = open.parent;
    } else if (tag) {
      const attrs: Record<string, string> = {};
      for (const attribute of (attributes ?? "").matchAll(/([^\s=>/]+)(?:="([^"]*)")?/g)) attrs[attribute[1] ?? ""] = decode(attribute[2] ?? "");
      const node: HtmlNode = { tag: tag.toLowerCase(), attrs, children: [], text: "", parent: current };
      current.children.push(node);
      if (!VOID.has(node.tag) && !selfClosing) current = node;
    }
  }
  return root;
}

export function walk(node: HtmlNode, visit: (node: HtmlNode) => void): void {
  for (const child of node.children) {
    visit(child);
    walk(child, visit);
  }
}

export const all = (root: HtmlNode, test: (node: HtmlNode) => boolean): HtmlNode[] => {
  const found: HtmlNode[] = [];
  walk(root, (node) => { if (test(node)) found.push(node); });
  return found;
};

/** The text a screen reader would read: `aria-hidden` subtrees are skipped, images count by their `alt`. */
export function textOf(node: HtmlNode): string {
  if (node.tag === "#text") return node.text;
  if (node.attrs["aria-hidden"] === "true") return "";
  if (node.tag === "img") return node.attrs.alt ?? "";
  return node.children.map(textOf).join(" ").replace(/\s+/g, " ").trim();
}

export function accessibleName(node: HtmlNode, root: HtmlNode): string {
  const direct = node.attrs["aria-label"];
  if (direct) return direct;
  const labelledby = node.attrs["aria-labelledby"];
  if (labelledby) {
    return labelledby.split(/\s+/).map((id) => all(root, (candidate) => candidate.attrs.id === id).map(textOf).join(" ")).join(" ").trim();
  }
  if (node.attrs.id) {
    const label = all(root, (candidate) => candidate.tag === "label" && candidate.attrs.for === node.attrs.id)[0];
    if (label) return textOf(label);
  }
  for (let ancestor = node.parent; ancestor; ancestor = ancestor.parent) if (ancestor.tag === "label") return textOf(ancestor);
  if (node.tag === "button" || node.tag === "a" || node.tag === "summary" || node.attrs.role === "tab" || node.attrs.role === "listitem") return textOf(node) || (node.attrs.title ?? "");
  return node.attrs.title ?? "";
}
