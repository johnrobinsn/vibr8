# TODO

## Terminal Sessions

- [ ] Fix xterm.js scrollbar gap when hidden in alternate buffer mode (tmux/vim). xterm.js reserves 15px for scrollbar gutter even when CSS hides it ([xtermjs#3074](https://github.com/xtermjs/xterm.js/issues/3074)). Current `.xterm-screen { width: 100% !important }` override doesn't fully work.
