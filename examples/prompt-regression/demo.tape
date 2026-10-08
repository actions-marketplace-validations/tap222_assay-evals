# Records docs/assets/assay-demo.gif, the README's demo. With assay-server and pytest installed:
#   brew install vhs && cd examples/prompt-regression && vhs demo.tape
Output ../../docs/assets/assay-demo.gif
Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 17
Set Width 1320
Set Height 780
Set Padding 26
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set WindowBar Colorful

Hide
Type "rm -rf .assay && export ASSAY_DEMO_PROMPT=1 PS1='$ ' && clear"
Enter
Sleep 1.5s
Show

Type "# A support agent and its tests. They pass."
Enter
Type "pytest -q --assay tests"
Enter
Sleep 3.5s

Hide
Type "clear"
Enter
Show
Type "# Someone adds one line to the prompt."
Enter
Type "diff prompts/support-1.txt prompts/support-2.txt"
Enter
Sleep 3s

Hide
Type "export ASSAY_DEMO_PROMPT=2 && clear"
Enter
Show
Type "# Answers still look right. Run the tests again:"
Enter
Type "pytest -q --assay tests"
Enter
Sleep 5.5s

Hide
Type "clear"
Enter
Show
Type "# Which case broke, how, and what caused it:"
Enter
Type "assay diff"
Enter
Sleep 9s
