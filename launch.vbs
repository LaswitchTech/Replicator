' Replicator Windows launcher (no console)
' - Prefers launching the built binary if present
' - Falls back to launching Git Bash + launch.sh if available

Option Explicit

Dim shell, fso, scriptDir, exePath, bashPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Try built EXE first
exePath = scriptDir & "\\dist\\windows\\Replicator.exe"
If fso.FileExists(exePath) Then
  shell.CurrentDirectory = scriptDir
  shell.Run Chr(34) & exePath & Chr(34), 1, False
  WScript.Quit 0
End If

' Fallback: Git Bash launch.sh (if user is in a dev checkout)
bashPath = shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\\Git\\bin\\bash.exe"
If fso.FileExists(bashPath) Then
  cmd = Chr(34) & bashPath & Chr(34) & " -lc " & Chr(34) & "cd \"" & scriptDir & "\" && ./launch.sh" & Chr(34)
  shell.Run cmd, 0, False
End If
