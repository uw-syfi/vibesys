#!/usr/bin/env node
// A syntactic guard plus TypeScript's never proof: a new generated case breaks tsc, even when
// a default would otherwise silently discard it. Dispatch comparisons use the same proof.
// Does not catch includes or Set.has on the case, nested destructuring, or casts to string.
import {readdirSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import {workspaceLayout} from './workspace_layout.mjs';

function unparenthesized(expr) {
  while (ts.isParenthesizedExpression(expr)) expr = expr.expression;
  return expr;
}
function caseSubject(expr, file, aliases, key, seen = new Set()) {
  const access = unparenthesized(expr);
  if (isCase(access)) return access.expression.getText(file);
  if (!ts.isIdentifier(access)) return undefined;
  const identity = key(access);
  if (seen.has(identity)) return undefined;
  seen.add(identity);
  const alias = aliases.get(identity);
  if (typeof alias === 'string') return alias;
  return alias ? caseSubject(alias, file, aliases, key, seen) : undefined;
}
function addCaseAlias(node, aliases, file, key) {
  if (!ts.isVariableDeclaration(node) || !node.initializer) return;
  if (ts.isIdentifier(node.name)) {
    aliases.set(key(node.name), node.initializer);
    return;
  }
  if (!ts.isObjectBindingPattern(node.name)) return;
  for (const binding of node.name.elements) {
    if ((binding.propertyName ?? binding.name).getText(file).replaceAll(/['"]/g, '') === 'case')
      aliases.set(key(binding.name), node.initializer.getText(file));
  }
}
function caseAliases(file, key) {
  const aliases = new Map();
  const collect = node => {
    addCaseAlias(node, aliases, file, key);
    ts.forEachChild(node, collect);
  };
  collect(file);
  return aliases;
}
const comparisonOperators = new Set([
  ts.SyntaxKind.EqualsEqualsToken,
  ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken,
  ts.SyntaxKind.ExclamationEqualsEqualsToken,
]);
function openCase(expr, checker) {
  if (!checker) return false;
  const type = checker.getTypeAtLocation(expr);
  const members = type.isUnion() ? type.types : [type];
  return members.some(member => (member.flags & ts.TypeFlags.String) !== 0);
}
function isCase(expr) {
  return (
    (ts.isPropertyAccessExpression(expr) && expr.name.text === 'case') ||
    (ts.isElementAccessExpression(expr) &&
      ts.isStringLiteral(expr.argumentExpression) &&
      expr.argumentExpression.text === 'case')
  );
}
function hasProof(node, file, subject) {
  const fallback = node.caseBlock.clauses.find(ts.isDefaultClause);
  return (
    fallback?.statements.some(
      statement =>
        ts.isExpressionStatement(statement) &&
        ts.isSatisfiesExpression(statement.expression) &&
        statement.expression.expression.getText(file) === subject &&
        statement.expression.type.kind === ts.SyntaxKind.NeverKeyword,
    ) ?? false
  );
}
function enclosedByProof(node, file, subject, subjectOf) {
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (
      ts.isSwitchStatement(parent) &&
      subjectOf(parent.expression) === subject &&
      hasProof(parent, file, subject)
    )
      return true;
  }
  return false;
}
export function readerDiagnostics(name, source, context) {
  const file = context?.file ?? ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true);
  const key = expr => context?.checker.getSymbolAtLocation(expr) ?? expr.getText(file);
  const aliases = caseAliases(file, key);
  const subjectOf = expr => caseSubject(expr, file, aliases, key);
  const found = [];
  const report = (node, message) => {
    const {line} = file.getLineAndCharacterOfPosition(node.getStart(file));
    found.push(`${name}:${line + 1}: ${message}`);
  };
  const visit = node => {
    if (ts.isSwitchStatement(node) && subjectOf(node.expression)) {
      const subject = subjectOf(node.expression);
      if (!hasProof(node, file, subject))
        report(
          node,
          `oneof switch needs default: ${subject} satisfies never; so tsc checks every kind`,
        );
    }
    if (ts.isBinaryExpression(node) && comparisonOperators.has(node.operatorToken.kind)) {
      const access = [node.left, node.right].find(expr => subjectOf(expr) !== undefined);
      const subject = access && subjectOf(access);
      if (
        subject &&
        !openCase(access, context?.checker) &&
        !enclosedByProof(node, file, subject, subjectOf)
      )
        report(
          node,
          'oneof comparisons need an exhaustive switch with satisfies never, or oneofIs with every kind declared',
        );
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

function scan(directory) {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    const path = join(directory, entry.name);
    if (['node_modules', 'dist', 'generated', '.git'].includes(entry.name)) return [];
    if (entry.isDirectory()) return scan(path);
    return /\.[cm]?tsx?$/.test(path) ? [path] : [];
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const files = workspaceLayout(process.cwd()).scanRoots.flatMap(scan);
  if (files.length === 0) {
    console.error('oneof reader check found no files to scan');
    process.exit(1);
  }
  const program = ts.createProgram(files, {
    strict: true,
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    customConditions: ['@vibesys/source'],
    jsx: ts.JsxEmit.ReactJSX,
    skipLibCheck: true,
  });
  const checker = program.getTypeChecker();
  const errors = files.flatMap(name => {
    const file = program.getSourceFile(name);
    return readerDiagnostics(name, readFileSync(name, 'utf8'), {file, checker});
  });
  for (const error of errors) console.error(error);
  process.exitCode = errors.length ? 1 : 0;
}
