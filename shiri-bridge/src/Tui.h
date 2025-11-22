#pragma once

#include <string>

// Minimal public interface for the terminal UI. The implementation lives in
// Tui.cpp and owns all terminal drawing, keyboard handling, and log windows.
//
// Core logic (discovery, RAOP streaming, shairport processes) interacts with
// the UI only via these functions.

namespace Tui {

// Start the interactive terminal UI loop.
// This call blocks until the global `running` flag (from AppState.h) becomes
// false or the user chooses to quit from the UI.
void Run();

// Update the status line shown at the bottom of the UI.
void SetStatus(const std::string& message);

// Inform the UI that core state has changed (e.g. discovery updated speakers).
// The next iteration of the UI loop will trigger a re-render.
void RequestRefresh();

// Append log lines to the various log panes. These functions are safe to call
// from other threads; they copy the string and schedule a UI refresh.
void AppendRaopLog(const std::string& line);
void AppendShairportLog(const std::string& line);
void AppendLibraopLog(const std::string& line);

} // namespace Tui
