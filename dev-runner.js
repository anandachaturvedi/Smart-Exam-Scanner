const { spawn } = require("child_process");
const path = require("path");

const isWindows = process.platform === "win32";
const npmCommand = isWindows ? "npm.cmd" : "npm";
const pythonCommand = "python";
const rootDir = __dirname;

const children = [];

function startProcess(name, command, args, options = {}) {
  const child = spawn(command, args, {
    stdio: "pipe",
    shell: isWindows,
    ...options,
  });

  children.push(child);

  child.stdout.on("data", (data) => {
    process.stdout.write(`[${name}] ${data}`);
  });

  child.stderr.on("data", (data) => {
    process.stderr.write(`[${name}] ${data}`);
  });

  child.on("exit", (code) => {
    if (shuttingDown) {
      return;
    }

    console.log(`[${name}] exited with code ${code ?? "unknown"}`);
    shutdown(code ?? 0);
  });

  child.on("error", (error) => {
    console.error(`[${name}] failed to start: ${error.message}`);
    shutdown(1);
  });

  return child;
}

let shuttingDown = false;

function shutdown(exitCode = 0) {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;

  for (const child of children) {
    if (!child.killed) {
      child.kill();
    }
  }

  setTimeout(() => process.exit(exitCode), 300);
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

startProcess("backend", pythonCommand, ["-m", "flask", "--app", "backend/app.py", "run", "--port", "5000", "--no-reload"], {
  cwd: rootDir,
});

startProcess("frontend", npmCommand, ["start"], {
  cwd: path.join(rootDir, "frontend"),
});
