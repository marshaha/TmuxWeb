/// A star button that reads and toggles the board favorite set, following
/// web's shell toggleStar: DELETE when starred, POST {pane} otherwise, then
/// reload the set so the button reflects the server, not optimism.
/// Load failure is silent, like web's loadFavorites catch.
library;

import 'package:flutter/material.dart';

import '../api/api_client.dart';
import '../theme.dart';

class FavoriteStarButton extends StatefulWidget {
  const FavoriteStarButton({super.key, required this.api, required this.paneRef});

  final Api api;
  final String paneRef;

  @override
  State<FavoriteStarButton> createState() => _FavoriteStarButtonState();
}

class _FavoriteStarButtonState extends State<FavoriteStarButton> {
  bool _starred = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.api.homeFavorites();
      final list = data['favorites'];
      if (list is! List || !mounted) return;
      final panes = list.map((item) => item is Map ? '${item['pane'] ?? ''}' : '$item').toSet();
      setState(() => _starred = panes.contains(widget.paneRef));
    } catch (_) {}
  }

  Future<void> _toggle() async {
    final starred = _starred;
    try {
      if (starred) {
        await widget.api.deleteHomeFavorite(widget.paneRef);
      } else {
        await widget.api.addHomeFavorite(widget.paneRef);
      }
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(starred ? '已取消收藏' : '已收藏到主页')));
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return IconButton(
      tooltip: _starred ? '取消收藏' : '收藏到主页',
      icon: Icon(
        _starred ? Icons.star : Icons.star_border,
        size: 20,
        color: _starred ? AppTheme.accent : null,
      ),
      onPressed: _toggle,
    );
  }
}
