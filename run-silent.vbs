' ============================================================
'  Silent launcher: starts the file manager with NO console
'  window and NO flicker at all (run.bat flashes a cmd window).
'  It only starts the program; it never touches your files.
' ============================================================
Option Explicit
Dim fso, sh, base, app, args, i, exe, list, found, rc

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
app  = base & "\file_manager.py"

args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " """ & WScript.Arguments(i) & """"
Next

If Not fso.FileExists(app) Then
    MsgBox "file_manager.py was not found next to this script.", 16, "File Manager"
    WScript.Quit 1
End If

list = Array("pythonw.exe", "pyw.exe", "python.exe", "py.exe")
found = ""
For Each exe In list
    ' run "where" with a hidden window, so nothing flashes on screen
    rc = sh.Run("cmd /c where " & exe & " >nul 2>nul", 0, True)
    If rc = 0 Then
        found = exe
        Exit For
    End If
Next

If found = "" Then
    MsgBox "Python 3.8+ was not found." & vbCrLf & _
           "Install it from https://www.python.org/downloads/windows/" & vbCrLf & _
           "and tick ""Add python.exe to PATH"".", 16, "File Manager"
    WScript.Quit 1
End If

sh.CurrentDirectory = base
sh.Run """" & found & """ """ & app & """" & args, 0, False