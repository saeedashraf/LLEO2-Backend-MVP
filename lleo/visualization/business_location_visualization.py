"""
Business Location Visualization for LLEO
Creates interactive maps and charts for business location analysis
"""

import folium
import matplotlib.pyplot as plt
import numpy as np
from typing import Dict, Any, List


class BusinessLocationVisualization:
    """Handles visualization for business location analysis"""

    def __init__(self, location: Dict, business_type: str, radius: int):
        """
        Initialize business location visualization

        Args:
            location: Location dictionary with lat/lon
            business_type: Type of business being searched
            radius: Search radius in meters
        """
        self.location = location
        self.business_type = business_type
        self.radius = radius

        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            self.center_lat = location['latitude']['center']
            self.center_lng = location['longitude']['center']
        else:
            self.center_lat = location['latitude']
            self.center_lng = location['longitude']

        self.location_name = location.get('name', 'Unknown')

    def create_interactive_map(self, businesses: List[Dict], save_path: str = 'business_map.html') -> None:
        """
        Create interactive Folium map with business markers

        Args:
            businesses: List of business dictionaries
            save_path: Path to save the HTML map file
        """
        print("🗺️  Creating interactive business location map...")

        # Create base map
        m = folium.Map(location=[self.center_lat, self.center_lng], zoom_start=14)

        # Add base layer
        folium.TileLayer('OpenStreetMap').add_to(m)

        # Add business markers
        for business in businesses:
            # Determine marker color based on rating if available
            if business.get('rating'):
                color = 'green' if business['rating'] >= 4 else 'orange' if business['rating'] >= 3 else 'red'
            else:
                color = 'blue'  # Default for businesses without ratings

            # Build popup text dynamically based on available data
            popup_parts = [f"<b>{business['name']}</b>"]

            if business.get('rating'):
                popup_parts.append(f"Rating: {business['rating']} ⭐ ({business['rating_count']} reviews)")

            if business.get('price_level'):
                popup_parts.append(f"Price: {business['price_level']}")

            popup_parts.append(f"Address: {business['address']}")

            if business.get('phone') and business['phone'] != 'N/A':
                popup_parts.append(f"Phone: {business['phone']}")

            if business.get('website') and business['website'] != 'N/A':
                popup_parts.append(f"<a href='{business['website']}' target='_blank'>Website</a>")

            if business.get('types'):
                popup_parts.append(f"Type: {', '.join(business['types'][:3])}")

            popup_text = "<br>".join(popup_parts)

            # Choose icon based on business type
            icon = self._get_icon_for_business(business.get('types', []))

            folium.Marker(
                [business['latitude'], business['longitude']],
                popup=popup_text,
                icon=folium.Icon(color=color, icon=icon)
            ).add_to(m)

        # Add search radius circle
        folium.Circle(
            location=[self.center_lat, self.center_lng],
            radius=self.radius,
            color='blue',
            fill=False,
            weight=2,
            dash_array='5, 5',
            popup=f'Search Radius: {self.radius}m'
        ).add_to(m)

        # Add center marker
        folium.Marker(
            [self.center_lat, self.center_lng],
            popup=f"<b>Search Center</b><br>{self.location_name}",
            icon=folium.Icon(color='darkblue', icon='star')
        ).add_to(m)

        # Save map
        m.save(save_path)
        print(f"✅ Interactive map saved to {save_path}")

    def _get_icon_for_business(self, types: List[str]) -> str:
        """Get appropriate icon based on business type"""
        type_icons = {
            'restaurant': 'cutlery',
            'cafe': 'coffee',
            'bank': 'usd',
            'hospital': 'plus-sign',
            'pharmacy': 'medkit',
            'school': 'education',
            'gas_station': 'road',
            'hotel': 'bed',
            'shopping_mall': 'shopping-cart',
            'gym': 'heart',
            'library': 'book',
            'parking': 'road',
            'atm': 'credit-card',
            'store': 'shopping-cart',
            'shop': 'shopping-cart'
        }

        for business_type in types:
            for key, icon in type_icons.items():
                if key in business_type.lower():
                    return icon

        return 'info-sign'  # Default icon

    def create_charts(self, businesses: List[Dict], save_path: str = 'business_charts.png') -> None:
        """
        Create business analysis charts

        Args:
            businesses: List of business dictionaries
            save_path: Path to save the PNG chart file
        """
        print("📊 Creating business analysis charts...")

        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 10))
        fig.suptitle(
            f'Business Location Analysis: {self.business_type}\n{self.location_name}',
            fontsize=16,
            fontweight='bold'
        )

        # Chart 1: Top businesses by rating (if ratings exist)
        businesses_with_ratings = [b for b in businesses if b.get('rating')]

        if businesses_with_ratings:
            businesses_with_ratings.sort(key=lambda x: x['rating'], reverse=True)
            names = [b['name'][:25] for b in businesses_with_ratings[:10]]
            ratings = [b['rating'] for b in businesses_with_ratings[:10]]

            ax1.barh(names, ratings, color='skyblue')
            ax1.set_xlabel('Rating', fontweight='bold')
            ax1.set_title('Top 10 Businesses by Rating', fontsize=12, fontweight='bold')
            ax1.grid(True, alpha=0.3, axis='x')
            ax1.set_xlim(0, 5)
        else:
            # If no ratings, show business count by type
            types_count = {}
            for b in businesses:
                for t in b.get('types', ['Unknown'])[:1]:  # Take first type
                    types_count[t] = types_count.get(t, 0) + 1

            if types_count:
                sorted_types = sorted(types_count.items(), key=lambda x: x[1], reverse=True)[:5]
                ax1.bar([t[0][:20] for t in sorted_types], [t[1] for t in sorted_types], color='skyblue')
                ax1.set_ylabel('Count', fontweight='bold')
                ax1.set_title('Business Types Distribution', fontsize=12, fontweight='bold')
                ax1.tick_params(axis='x', rotation=45)
                ax1.grid(True, alpha=0.3, axis='y')

        # Chart 2: Rating distribution (if ratings exist)
        if businesses_with_ratings:
            ratings_all = [b['rating'] for b in businesses_with_ratings]
            rating_bins = [0, 2, 3, 4, 5]
            rating_counts = np.histogram(ratings_all, bins=rating_bins)[0]
            labels = ['Poor (0-2)', 'Fair (2-3)', 'Good (3-4)', 'Excellent (4-5)']
            colors = ['#ff0000', '#ff7e00', '#ffff00', '#00e400']

            non_zero_indices = [i for i, count in enumerate(rating_counts) if count > 0]
            if non_zero_indices:
                filtered_counts = [rating_counts[i] for i in non_zero_indices]
                filtered_labels = [labels[i] for i in non_zero_indices]
                filtered_colors = [colors[i] for i in non_zero_indices]

                ax2.pie(filtered_counts, labels=filtered_labels, autopct='%1.1f%%', colors=filtered_colors)
                ax2.set_title('Rating Distribution', fontsize=12, fontweight='bold')
            else:
                ax2.text(0.5, 0.5, 'No rating data available', ha='center', va='center')
                ax2.set_title('Rating Distribution', fontsize=12, fontweight='bold')
        else:
            # Show business status distribution
            status_count = {}
            for b in businesses:
                status = b.get('business_status', 'Unknown')
                status_count[status] = status_count.get(status, 0) + 1

            if status_count:
                ax2.pie(status_count.values(), labels=status_count.keys(), autopct='%1.1f%%')
                ax2.set_title('Business Status', fontsize=12, fontweight='bold')
            else:
                ax2.text(0.5, 0.5, f'{len(businesses)} Businesses Found', ha='center', va='center', fontsize=14)
                ax2.set_title('Business Status', fontsize=12, fontweight='bold')

        # Chart 3: Distance distribution
        if businesses:
            distances = []
            for b in businesses:
                # Simple distance calculation (not exact but good for visualization)
                lat_diff = b['latitude'] - self.center_lat
                lng_diff = b['longitude'] - self.center_lng
                dist = np.sqrt(lat_diff**2 + lng_diff**2) * 111  # rough km conversion
                distances.append(dist)

            ax3.hist(distances, bins=10, color='coral', edgecolor='black')
            ax3.set_xlabel('Distance from Center (km)', fontweight='bold')
            ax3.set_ylabel('Count', fontweight='bold')
            ax3.set_title('Distance Distribution', fontsize=12, fontweight='bold')
            ax3.grid(True, alpha=0.3, axis='y')
        else:
            ax3.text(0.5, 0.5, 'No distance data available', ha='center', va='center')
            ax3.set_title('Distance Distribution', fontsize=12, fontweight='bold')

        # Chart 4: Most reviewed (if reviews exist) or business summary
        if businesses_with_ratings:
            businesses_with_ratings.sort(key=lambda x: x.get('rating_count', 0), reverse=True)
            top_reviewed = businesses_with_ratings[:10]
            review_counts = [b['rating_count'] for b in top_reviewed]
            names = [b['name'][:15] for b in top_reviewed]

            ax4.barh(names, review_counts, color='coral')
            ax4.set_xlabel('Review Count', fontweight='bold')
            ax4.set_title('Top 10 Most Reviewed', fontsize=12, fontweight='bold')
            ax4.grid(True, alpha=0.3, axis='x')
        else:
            # Just show business count summary
            ax4.text(0.5, 0.6, f'Total Businesses Found: {len(businesses)}',
                    ha='center', va='center', fontsize=20, fontweight='bold')
            ax4.text(0.5, 0.4, f'Analysis Complete',
                    ha='center', va='center', fontsize=14)
            ax4.set_xlim(0, 1)
            ax4.set_ylim(0, 1)
            ax4.axis('off')

        plt.tight_layout()
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()

        print(f"✅ Charts saved to {save_path}")
