from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Game, Move

class MoveInline(admin.TabularInline):
    model = Move
    extra = 0
    readonly_fields = ('created_at',)
    fields = ('move_number', 'rolled', 'from_cell', 'to_cell', 'event_type', 'note', 'created_at')
    ordering = ('move_number',)

@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('id', 'player', 'game_type', 'game_name', 'status', 'is_active',
                    'last_move_number', 'current_cell', 'started_at', 'updated_at')
    list_filter = ('status', 'is_active', 'game_type')
    search_fields = ('id', 'player__email', 'player__telegram_username', 'game_name')
    readonly_fields = ('started_at', 'updated_at', 'finished_at', 'last_move_number')
    inlines = [MoveInline]

@admin.register(Move)
class MoveAdmin(admin.ModelAdmin):
    list_display = ('game', 'move_number', 'rolled', 'from_cell', 'to_cell', 'event_type', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('game__id',)
    ordering = ('game', 'move_number')
