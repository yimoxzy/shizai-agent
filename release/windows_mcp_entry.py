"""PyInstaller entry point for the Windows MCP executable."""

from shizai_agent.core.std_streams import install_shutdown_safe_stderr

install_shutdown_safe_stderr()

from shizai_agent.core.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
