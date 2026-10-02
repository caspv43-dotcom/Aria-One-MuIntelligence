# Bundled fonts

`DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `DejaVuSerif-Bold.ttf`

mvfx burns captions with libass, which normally finds fonts through fontconfig.
Containers and minimal images often have no fontconfig cache (or no fonts at
all), which makes the `subtitles` filter render nothing at all. Shipping the
fonts here and passing them via `subtitles=…:fontsdir=assets/fonts` removes
that dependency, so `mvfx lyrics` works on a bare machine.

Copied from the `fonts-dejavu-core` Debian package.

Licence: the DejaVu fonts are released under a permissive licence derived from
the Bitstream Vera Fonts licence - redistribution and modification are allowed,
provided the fonts are not sold by themselves and the copyright notice is kept.
See <https://dejavu-fonts.github.io/License.html>.
