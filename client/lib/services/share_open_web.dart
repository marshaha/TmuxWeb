import 'package:web/web.dart' as web;

/// Opens [url] in a new browser tab, exactly like web's shares.html
/// `<a href="..." target="_blank">`.
void openShareTab(String url) {
  web.window.open(url, '_blank');
}
