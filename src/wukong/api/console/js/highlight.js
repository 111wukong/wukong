/* Just enough syntax colour to make code scannable.
 *
 * Not a parser and not a highlighter library: one regex per token class, and
 * every token is inserted as a text node, so nothing here can inject markup.
 * The goal is recognition, not accuracy -- a keyword in a different colour and
 * comments faded out is what turns a wall of monospace into something you can
 * read at a glance.
 *
 * A token it gets wrong is a token in the wrong colour, which is the same
 * failure mode as no colour at all. That is the reason it is allowed to be
 * approximate.
 */

const KEYWORDS = {
  python: "def class return if elif else for while import from as with try except finally raise yield lambda pass break continue global nonlocal assert del in is not and or None True False async await self match case",
  js: "function const let var return if else for while import from export default class extends new try catch finally throw async await of in typeof instanceof null undefined true false this super yield delete void",
  ts: "function const let var return if else for while import from export default class extends new try catch finally throw async await of in typeof instanceof null undefined true false this super yield delete void interface type enum implements readonly public private protected",
  go: "func package import return if else for range switch case default defer go chan select map struct interface type var const nil true false break continue",
  rust: "fn let mut pub use mod struct enum impl trait match if else for while loop return self Self true false Some None Ok Err where as const static ref move async await",
  json: "true false null",
  yaml: "true false null",
  shell: "if then else fi for while do done case esac function export local return echo cd set unset",
  sql: "select from where join left right inner outer group by order limit insert update delete into values create table drop alter index on as and or not null",
};

const ALIASES = {
  py: "python",
  python3: "python",
  javascript: "js",
  jsx: "js",
  tsx: "ts",
  typescript: "ts",
  golang: "go",
  rs: "rust",
  bash: "shell",
  sh: "shell",
  zsh: "shell",
  yml: "yaml",
  toml: "yaml",
  ini: "yaml",
};

const CLASS = {
  comment: "tok-c",
  string: "tok-s",
  number: "tok-n",
  keyword: "tok-k",
  def: "tok-d",
  punct: "tok-p",
};

function family(lang) {
  const key = String(lang || "").toLowerCase();
  return ALIASES[key] || key;
}

function pattern(lang) {
  const words = KEYWORDS[lang];
  const parts = [
    // Comments first: a `#` inside a string must not start one, and a string
    // inside a comment must not end it. Alternation order is the only
    // precedence this gets.
    "(?<comment>\\/\\/[^\\n]*|#[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/)",
    "(?<string>\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`)",
    "(?<number>\\b\\d+(?:\\.\\d+)?\\b)",
  ];
  if (words) {
    // `def name` / `fn name` / `function name` -- the name matters more than
    // the keyword, so it gets its own class.
    parts.push("(?<def>\\b(?:def|fn|func|function|class)\\s+[A-Za-z_][\\w]*)");
    parts.push(`(?<keyword>\\b(?:${words.trim().split(/\s+/).join("|")})\\b)`);
  }
  return new RegExp(parts.join("|"), "g");
}

const CACHE = new Map();

function compiled(lang) {
  const key = family(lang) || "plain";
  if (!CACHE.has(key)) CACHE.set(key, pattern(key));
  return CACHE.get(key);
}

/** Append `text` to `parent` with tokens wrapped in spans. */
export function paint(parent, text, lang) {
  const source = String(text ?? "");
  if (!lang || source.length > 200_000) {
    parent.append(document.createTextNode(source));
    return parent;
  }
  const re = compiled(lang);
  re.lastIndex = 0;
  let last = 0;
  let match;
  while ((match = re.exec(source)) !== null) {
    if (match.index > last) {
      parent.append(document.createTextNode(source.slice(last, match.index)));
    }
    const groups = match.groups || {};
    const kind = Object.keys(groups).find((name) => groups[name] !== undefined) || "";
    const span = document.createElement("span");
    span.className = CLASS[kind] || "";
    span.textContent = match[0];
    parent.append(span);
    last = match.index + match[0].length;
    if (match[0].length === 0) re.lastIndex += 1; // never loop on an empty match
  }
  if (last < source.length) parent.append(document.createTextNode(source.slice(last)));
  return parent;
}

/** One line at a time, for viewers that render a gutter per line. */
export function paintLine(parent, text, lang) {
  return paint(parent, text, lang);
}
