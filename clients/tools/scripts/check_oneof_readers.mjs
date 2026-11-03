#!/usr/bin/env node
// A syntactic guard plus TypeScript's never proof: a new generated case breaks tsc, even when
// a default would otherwise silently discard it. Also keep test assertions outside respond,
// whose caller may catch exceptions or never invoke it.
import {readdirSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import {workspaceLayout} from './workspace_layout.mjs';

export function readerDiagnostics(name, source) {
  const file = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true);
  const found = [];
  const report = (node, message) => {
    const {line} = file.getLineAndCharacterOfPosition(node.getStart(file));
    found.push(`${name}:${line + 1}: ${message}`);
  };
  const visit = node => {
    if (
      ts.isSwitchStatement(node) &&
      ((ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'case') ||
        (ts.isElementAccessExpression(node.expression) &&
          ts.isStringLiteral(node.expression.argumentExpression) &&
          node.expression.argumentExpression.text === 'case'))
    ) {
      const subject = node.expression.expression.getText(file);
      const fallback = node.caseBlock.clauses.find(ts.isDefaultClause);
      const proof = fallback?.statements.some(
        statement =>
          ts.isExpressionStatement(statement) &&
          ts.isSatisfiesExpression(statement.expression) &&
          statement.expression.expression.getText(file) === subject &&
          statement.expression.type.kind === ts.SyntaxKind.NeverKeyword,
      );
      if (!proof)
        report(
          node,
          `oneof switch needs default: ${subject} satisfies never; so tsc checks every kind`,
        );
    }
    if (
      (ts.isPropertyAssignment(node) || ts.isMethodDeclaration(node)) &&
      node.name?.getText(file).replaceAll(/['"]/g, '') === 'respond'
    ) {
      const check = child => {
        if (ts.isCallExpression(child) && child.expression.getText(file) === 'expect')
          report(
            child,
            'record observations in respond and assert after the Fake finishes; callback throws may be caught',
          );
        ts.forEachChild(child, check);
      };
      ts.forEachChild(node, check);
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
    return /\.[cm]?tsx?$/.test(path) ? readerDiagnostics(path, readFileSync(path, 'utf8')) : [];
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const errors = workspaceLayout(process.cwd()).scanRoots.flatMap(scan);
  for (const error of errors) console.error(error);
  process.exitCode = errors.length ? 1 : 0;
}
