// Subprocess manager for the Linux bridge helper.
// Newline-delimited JSON request/response over stdio.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import os from "node:os";
import path from "node:path";

const HELPER_PATH = path.join(os.homedir(), ".pi", "agent", "helpers", "linux-computer-use", "bridge");
const TIMEOUT_MS = 15000;

interface Pending {
	resolve: (v: any) => void;
	reject: (e: Error) => void;
	timer: NodeJS.Timeout;
}

let helper: ChildProcessWithoutNullStreams | undefined;
let stdoutBuf = "";
let seq = 0;
const pending = new Map<string, Pending>();

function rejectAll(err: Error) {
	for (const [id, p] of pending) {
		clearTimeout(p.timer);
		p.reject(err);
		pending.delete(id);
	}
}

function onLine(line: string) {
	const trimmed = line.trim();
	if (!trimmed) return;
	let msg: any;
	try {
		msg = JSON.parse(trimmed);
	} catch {
		return;
	}
	const id = msg.id as string | undefined;
	if (!id) return;
	const p = pending.get(id);
	if (!p) return;
	clearTimeout(p.timer);
	pending.delete(id);
	if (msg.ok) p.resolve(msg.result);
	else p.reject(new Error(String(msg.error ?? "bridge error")));
}

function ensureHelper(): ChildProcessWithoutNullStreams {
	if (helper && helper.exitCode === null && !helper.killed) return helper;
	const child = spawn(HELPER_PATH, [], { stdio: ["pipe", "pipe", "pipe"] });
	child.stdout.setEncoding("utf8");
	child.stderr.setEncoding("utf8");
	child.stdin.setDefaultEncoding("utf8");
	child.stdout.on("data", (chunk: string) => {
		stdoutBuf += chunk;
		let idx;
		while ((idx = stdoutBuf.indexOf("\n")) >= 0) {
			const line = stdoutBuf.slice(0, idx);
			stdoutBuf = stdoutBuf.slice(idx + 1);
			onLine(line);
		}
	});
	child.stderr.on("data", (chunk: string) => {
		if (process.env.PCUL_DEBUG) process.stderr.write(`[pcul] ${chunk}`);
	});
	child.on("error", (e) => {
		if (helper === child) helper = undefined;
		rejectAll(new Error(`bridge crashed: ${e.message}`));
	});
	child.on("exit", (code, sig) => {
		if (helper === child) helper = undefined;
		rejectAll(new Error(`bridge exited (${sig ?? code})`));
	});
	helper = child;
	stdoutBuf = "";
	return child;
}

export async function send<T = any>(cmd: string, args: Record<string, unknown> = {}): Promise<T> {
	const id = `r${++seq}`;
	const child = ensureHelper();
	return new Promise<T>((resolve, reject) => {
		const timer = setTimeout(() => {
			pending.delete(id);
			reject(new Error(`bridge timeout: ${cmd}`));
		}, TIMEOUT_MS);
		pending.set(id, { resolve, reject, timer });
		child.stdin.write(`${JSON.stringify({ id, cmd, ...args })}\n`);
	});
}

export function stopBridge(): void {
	if (helper) {
		try {
			helper.stdin.end();
			helper.kill();
		} catch {}
		helper = undefined;
	}
	rejectAll(new Error("bridge stopped"));
}

// Typed wrappers
export const listWindows = () => send("list_windows");
export const screenshot = (opts: { window?: string } = {}) => send("screenshot", opts);
export const click = (opts: { ref?: string; x?: number; y?: number; button?: string; clickCount?: number }) => send("click", opts);
export const typeText = (opts: { text: string }) => send("type_text", opts);
export const setText = (opts: { ref: string; text: string }) => send("set_text", opts);
export const keypress = (opts: { keys: string[] }) => send("keypress", opts);
export const scroll = (opts: { ref?: string; x?: number; y?: number; scrollY?: number; scrollX?: number }) => send("scroll", opts);
export const computerActions = (opts: { actions: any[] }) => send("computer_actions", opts);

// Format helper for tool results
import type { AgentToolResult } from "./types.ts";

export function ok(summary: string, details?: unknown): AgentToolResult {
	return { content: [{ type: "text", text: summary }], details: details as any };
}

export function okWithImage(summary: string, pngBase64: string, details?: unknown): AgentToolResult {
	return {
		content: [
			{ type: "text", text: summary },
			{ type: "image", data: pngBase64, mimeType: "image/png" },
		],
		details: details as any,
	};
}

export function err(message: string): AgentToolResult {
	return { content: [{ type: "text", text: `error: ${message}` }] };
}
