# VHS recording script for the README demo GIF.
#   vhs docs/demo.tape   →   docs/demo.gif

Output docs/demo.gif

Set FontSize 18
Set Width 1200
Set Height 180
Set Padding 24
Set Theme "Aardvark Blue"
Set Shell bash

# Hide setup keystrokes; only the statusline animation is visible in the GIF.
Hide
Type "clear"
Enter
Sleep 300ms
Type "bash docs/demo.sh"
Enter
Show

# One full cycle ≈ 9.5s; small tail for the final idle frame.
Sleep 11s
