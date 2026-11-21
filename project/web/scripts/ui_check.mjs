#!/usr/bin/env node
/**
 * 店诊 StoreDoctor · 执行台样式自检
 *
 * 零依赖、纯静态：不启动 Vite，也不解析 TS，只做「class 名」级别的对账。
 *
 *   1. 读 src/**\/*.css，收集全部类选择器（\.[A-Za-z][\w-]*）。
 *   2. 读 src/**\/*.tsx，解析 className 的值：
 *        className="a b"、className={'a b'}、className={`a ${x}`}
 *      只取字符串字面量与模板字符串里的静态片段；${...} 内部不求值。
 *   3. 报告三类问题：缺失 / 未使用 / 硬性缺失。
 *   4. 硬性缺失或有「缺失」→ 退出码 1；否则 0。
 *
 * 模板字符串里的动态后缀（如 `ax-row--${status}`）会得到静态片段 "ax-row--"，
 * 它以 "-" 结尾，本脚本把它当成「动态前缀」而不是类名：
 *   - 不参与「缺失」判断（"ax-row--" 本身当然不在 CSS 里）；
 *   - 参与「未使用」判断的前缀匹配（ax-row--drafted 等视为可能被用到）。
 * 这是静态检查在动态类名上的折中，避免 5 个状态修饰类被误报。
 *
 * 用法（在 project/web 下）：
 *   node scripts/ui_check.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

/* ------------------------------ 配置常量 ------------------------------ */

// 已经存在、由 styles.css / styles-settings.css 提供的类，允许 tsx 使用而 CSS 里不出现
const ALLOWED_MISSING = new Set(["chip", "btn", "note", "tabs", "sr-only"]);

// 执行台样式必须提供的类；缺任何一个都直接判失败
const REQUIRED = [
  "ax-approval",
  "ax-list",
  "ax-row",
  "ax-empty",
  "ax-skeleton",
  "axd",
  "axd__panel",
  "axd__actions",
  "axd__btn",
];

// 只关心执行台这两族类名，避免把全局样式算进「未使用」
const FAMILY_PREFIXES = ["ax-", "axd"];

const SKIP_DIRS = new Set(["node_modules", "dist", "build", ".git", "coverage"]);
const CLASS_RE = /\.([A-Za-z][\w-]*)/g;
const TOKEN_RE = /[a-z][\w-]*/g;

/* ------------------------------ 路径工具 ------------------------------ */

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIR, "..");
const SRC = path.join(ROOT, "src");

const rel = (file) => path.relative(ROOT, file).split(path.sep).join("/");

/** 递归收集 src 下指定扩展名的文件 */
function walk(dir, ext, out = []) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (entry.name.startsWith(".")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) walk(full, ext, out);
    } else if (entry.isFile() && entry.name.endsWith(ext)) {
      out.push(full);
    }
  }
  return out;
}

const byPath = (a, b) => a.localeCompare(b);

/* ------------------------------ CSS 侧 ------------------------------ */

/** 去掉注释但保留换行，避免类名出现在注释里被误判 */
function stripCssComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "));
}

/** 收集 CSS 里定义过的类名：name -> 首次出现的文件 */
function collectCssClasses(files) {
  const classes = new Map();
  for (const file of files) {
    let source;
    try {
      source = readFileSync(file, "utf8");
    } catch (error) {
      console.warn(`[警告] 读不了 ${rel(file)}：${error.message}`);
      continue;
    }
    const css = stripCssComments(source);
    for (const match of css.matchAll(CLASS_RE)) {
      const name = match[1];
      if (!classes.has(name)) classes.set(name, file);
    }
  }
  return classes;
}

/* ------------------------------ TSX 侧 ------------------------------ */

/** 从 start（引号本身）开始找字符串结尾，返回引号下标 */
function findStringEnd(source, start, quote) {
  let i = start + 1;
  while (i < source.length) {
    const ch = source[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === quote) return i;
    i += 1;
  }
  return source.length - 1;
}

/** 从 start（'`'）开始找模板字符串结尾；跳过 ${...} */
function findTemplateEnd(source, start) {
  let i = start + 1;
  while (i < source.length) {
    const ch = source[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === "`") return i;
    if (ch === "$" && source[i + 1] === "{") {
      i = skipBalanced(source, i + 1);
      continue;
    }
    i += 1;
  }
  return source.length - 1;
}

/** 从 start（'{'）开始做括号配平，返回配平后的下一个下标 */
function skipBalanced(source, start) {
  let depth = 0;
  let i = start;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '"' || ch === "'") {
      i = findStringEnd(source, i, ch) + 1;
      continue;
    }
    if (ch === "`") {
      i = findTemplateEnd(source, i) + 1;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      i += 1;
      continue;
    }
    if (ch === "}") {
      depth -= 1;
      i += 1;
      if (depth === 0) return i;
      continue;
    }
    i += 1;
  }
  return i;
}

/**
 * 找到所有 className=...，返回原始表达式文本（含外层引号 / 反引号 / 花括号）。
 * 支持 "..."、'...'、`...`、{...}（{...} 内部可嵌套字符串和模板）。
 */
function collectClassNameExpressions(source) {
  const expressions = [];
  const re = /\bclassName\s*=\s*/g;
  let match;
  while ((match = re.exec(source)) !== null) {
    let i = re.lastIndex;
    while (i < source.length && /\s/.test(source[i])) i += 1;
    const ch = source[i];
    let end;
    if (ch === '"' || ch === "'") {
      end = findStringEnd(source, i, ch) + 1;
    } else if (ch === "`") {
      end = findTemplateEnd(source, i) + 1;
    } else if (ch === "{") {
      end = skipBalanced(source, i);
    } else {
      continue;
    }
    expressions.push(source.slice(i, end));
    re.lastIndex = end;
  }
  return expressions;
}

/** 把模板字符串里的 ${...} 抹成空格，只留静态片段 */
function stripExpressions(text) {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\") {
      out += text[i + 1] ?? "";
      i += 2;
      continue;
    }
    if (ch === "$" && text[i + 1] === "{") {
      i = skipBalanced(text, i + 1);
      out += " ";
      continue;
    }
    out += ch;
    i += 1;
  }
  return out;
}

/**
 * 判断某个字符串字面量是否只是比较运算的操作数。
 * 例如 className={tab === "dashboard" ? "on" : ""} 里，"dashboard" 是 tab 的取值，
 * 不是类名；"on" / "" 才是三元分支里的类值。
 */
function isComparisonOperand(expression, start, end) {
  const before = expression.slice(0, start).trimEnd();
  const after = expression.slice(end + 1).trimStart();
  const operators = ["===", "!==", "==", "!=", "<=", ">=", "<", ">"];
  return operators.some((op) => before.endsWith(op) || after.startsWith(op));
}

/** 从 className 表达式里提取字符串字面量（含模板的静态片段） */
function extractLiterals(expression) {
  const literals = [];
  let i = 0;
  while (i < expression.length) {
    const ch = expression[i];
    if (ch === '"' || ch === "'") {
      const end = findStringEnd(expression, i, ch);
      if (!isComparisonOperand(expression, i, end)) {
        literals.push(expression.slice(i + 1, end));
      }
      i = end + 1;
      continue;
    }
    if (ch === "`") {
      const end = findTemplateEnd(expression, i);
      literals.push(stripExpressions(expression.slice(i + 1, end)));
      i = end + 1;
      continue;
    }
    i += 1;
  }
  return literals;
}

/**
 * 收集 tsx 里用到的类名。
 * exact：确定的类名；prefixes：以 "-" 结尾的动态前缀。
 * exact 记录来源文件，方便报告里回指。
 */
function collectTsxClasses(files) {
  const exact = new Map(); // name -> Set<file>
  const prefixes = new Set();
  const parsedFiles = [];

  for (const file of files) {
    let source;
    try {
      source = readFileSync(file, "utf8");
    } catch (error) {
      console.warn(`[警告] 读不了 ${rel(file)}：${error.message}`);
      continue;
    }
    parsedFiles.push(file);

    const literals = collectClassNameExpressions(source).flatMap(extractLiterals);
    for (const literal of literals) {
      for (const match of literal.matchAll(TOKEN_RE)) {
        const token = match[0];
        if (token.endsWith("-")) {
          // 模板动态后缀的静态前半截，如 ax-row-- / chip--
          prefixes.add(token);
          continue;
        }
        if (!exact.has(token)) exact.set(token, new Set());
        exact.get(token).add(file);
      }
    }
  }

  return { exact, prefixes, parsedFiles };
}

/* ------------------------------ 规则 ------------------------------ */

function isFamilyClass(name) {
  return FAMILY_PREFIXES.some((prefix) => name.startsWith(prefix));
}

function main() {
  const cssFiles = walk(SRC, ".css").sort(byPath);
  const tsxFiles = walk(SRC, ".tsx").sort(byPath);

  console.log("店诊 StoreDoctor · 执行台样式自检");
  console.log(`根目录：${ROOT}`);
  console.log("");

  if (cssFiles.length === 0) {
    console.warn("[提示] src 下没有找到 .css 文件，跳过类名收集。");
  }
  if (tsxFiles.length === 0) {
    console.warn("[提示] src 下没有找到 .tsx 文件，跳过组件类名收集。");
  }

  const expected = ["src/views/Approval.tsx", "src/views/ActionDetail.tsx"];
  for (const file of expected) {
    const exists = tsxFiles.some((candidate) => rel(candidate) === file);
    if (!exists) {
      console.warn(`[提示] 未找到 ${file}（可能还没写完），已跳过。`);
    }
  }
  if (cssFiles.length || tsxFiles.length) console.log("");

  const cssClasses = collectCssClasses(cssFiles);
  const { exact: usedExact, prefixes: usedPrefixes, parsedFiles } = collectTsxClasses(tsxFiles);

  const familyCss = [...cssClasses.keys()].filter(isFamilyClass).sort();

  /* 1. 硬性缺失：清单里的类在 CSS 里完全没有 */
  const hardMissing = REQUIRED.filter((name) => !cssClasses.has(name));

  /* 2. 缺失：tsx 用到、CSS 没有、且不在允许清单 */
  const missing = [];
  for (const [name, files] of usedExact) {
    if (cssClasses.has(name)) continue;
    if (ALLOWED_MISSING.has(name)) continue;
    missing.push([name, files]);
  }
  missing.sort((a, b) => a[0].localeCompare(b[0]));

  /* 3. 未使用：CSS 里定义了，但没有任何精确命中或动态前缀命中 */
  const unused = familyCss.filter((name) => {
    if (usedExact.has(name)) return false;
    return ![...usedPrefixes].some((prefix) => name.startsWith(prefix));
  });

  const missingTotal = new Set([...missing.map(([name]) => name), ...hardMissing]).size;

  /* ------------------------------ 报告 ------------------------------ */

  console.log(`CSS：${cssFiles.length} 个文件，共 ${cssClasses.size} 个类`);
  console.log(`TSX：${parsedFiles.length} 个文件`);
  console.log("");

  if (hardMissing.length > 0) {
    console.log(`硬性缺失（${hardMissing.length}）：${hardMissing.join("、")}`);
  } else {
    console.log("硬性缺失（0）：无");
  }

  if (missing.length > 0) {
    console.log(`缺失（${missing.length}）：`);
    for (const [name, files] of missing) {
      const sources = [...files].sort(byPath).map(rel).join("、");
      console.log(`  - ${name}  ← ${sources}`);
    }
  } else {
    console.log("缺失（0）：无");
  }

  console.log(
    `未使用（${unused.length}）：${unused.length > 0 ? unused.join("、") : "无"}`,
  );

  const activePrefixes = [...usedPrefixes]
    .filter((prefix) => familyCss.some((name) => name.startsWith(prefix)))
    .sort();
  if (activePrefixes.length > 0) {
    console.log(`动态前缀：${activePrefixes.join("、")}（按前缀命中的类不计入未使用）`);
  }

  console.log("");
  console.log(`问题：${missingTotal} 个缺失 / ${unused.length} 个未使用`);

  const failed = hardMissing.length > 0 || missing.length > 0;
  console.log(failed ? "结果：未通过" : "结果：通过");
  process.exitCode = failed ? 1 : 0;
}

main();
